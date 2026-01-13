import discord
from discord.ext import commands
import yt_dlp
import os
import json
import random
import asyncio
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

# =======================
# ENV
# =======================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
FIREBASE_KEY = os.getenv("FIREBASE_SERVICE_ACCOUNT")

# 🔴 본인 디스코드 ID (환경변수 OWNER_ID로 설정 가능)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# (선택) yt-dlp 쿠키 파일 경로 (403/Cloudflare 막힘 대비)
YTDLP_COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "").strip()

# =======================
# 자동 퇴장 설정값
# =======================
VOICE_IDLE_SECONDS = 300
WARNING_SECONDS = 30  # 지금은 미사용이지만 그대로 둠(요청: 삭제 금지)

# =======================
# Firebase
# =======================
if not firebase_admin._apps:
    if not FIREBASE_KEY:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT 환경변수가 비어있습니다.")
    cred_dict = json.loads(FIREBASE_KEY)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# =======================
# Discord
# =======================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =======================
# 상태
# =======================
current_team = {}
lineup_message = {}
idle_countdown_tasks = {}
now_playing_message = {}

ENTRANCE_ROLE_NAME = "등장곡 재생인"

# =======================
# 팀 / Firestore
# =======================
def get_team(guild_id):
    return current_team.get(guild_id, "A팀")

def team_ref(team, path):
    return db.collection("teams").document(team).collection(path)

# =======================
# 권한
# =======================
def is_admin(ctx):
    return ctx.author.guild_permissions.administrator

def has_entrance_role(member):
    return any(r.name == ENTRANCE_ROLE_NAME for r in member.roles)

def can_manage(ctx):
    return (
        is_admin(ctx)
        or has_entrance_role(ctx.author)
        or ctx.author.id == OWNER_ID
    )

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

def get_default_text_channel(guild: discord.Guild):
    """
    guild.text_channels[0]는 서버 설정/권한에 따라 실패할 수 있음.
    보낼 수 있는 채널을 하나 골라서 반환.
    """
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if perms.send_messages:
            return ch
    return None

async def connect_voice_by_guild(guild, channel=None, preferred_member: discord.Member = None):
    """
    - 이미 연결돼 있으면 그대로 반환
    - preferred_member가 음성채널에 있으면 그 채널로 연결
    - 아니면 길드 멤버 중 음성채널에 있는 사람 찾아 연결
    """
    if guild.voice_client:
        return guild.voice_client

    try:
        if preferred_member and preferred_member.voice and preferred_member.voice.channel:
            vc = await preferred_member.voice.channel.connect()
            if channel:
                await channel.send(f"🔊 음성 채널 연결: {preferred_member.voice.channel.name}")
            return vc
    except Exception as e:
        if channel:
            await channel.send(f"❌ 음성 채널 연결 실패(preferred): {e}")
        return None

    for m in guild.members:
        try:
            if m.voice and m.voice.channel:
                vc = await m.voice.channel.connect()
                if channel:
                    await channel.send(f"🔊 음성 채널 연결: {m.voice.channel.name}")
                return vc
        except Exception:
            continue

    if channel:
        await channel.send("❌ 음성 채널에 아무도 없음")
    return None

# =======================
# 볼륨
# =======================
def volume_ref(team):
    return db.collection("teams").document(team).collection("state").document("volume")

def get_volume(team):
    doc = volume_ref(team).get()
    if not doc.exists:
        volume_ref(team).set({"value": 0.5})
        return 0.5
    return doc.to_dict().get("value", 0.5)

def set_volume(team, value):
    volume_ref(team).set({"value": value})

# =======================
# 경기 상태
# =======================
def game_state(team):
    ref = db.collection("teams").document(team).collection("state").document("game")
    if not ref.get().exists:
        ref.set({"currentOrder": 1})
    return ref

# =======================
# Embed
# =======================
async def update_now_playing_embed(channel, guild_id, title, song, countdown=None):
    team = get_team(guild_id)
    volume = int(get_volume(team) * 100)
    order = game_state(team).get().to_dict().get("currentOrder", "-")

    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="🎵 현재 곡", value=song, inline=False)
    embed.add_field(name="🏷 팀", value=team, inline=True)
    embed.add_field(name="🔊 볼륨", value=f"{volume}%", inline=True)
    embed.add_field(name="⚾ 타순", value=f"{order}번", inline=True)

    if countdown is not None:
        embed.set_footer(text=f"⏱ 자동 퇴장까지 {countdown}초")

    if channel is None:
        return

    if guild_id not in now_playing_message:
        now_playing_message[guild_id] = await channel.send(embed=embed)
    else:
        try:
            await now_playing_message[guild_id].edit(embed=embed)
        except discord.NotFound:
            now_playing_message[guild_id] = await channel.send(embed=embed)

# =======================
# 오디오 (유튜브)
# =======================
# 유튜브/yt-dlp 이슈 대응: player_client 지정 + noplaylist + 쿠키 옵션 + 에러 로깅 강화
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "socket_timeout": 15,
    "source_address": "0.0.0.0",
    "noplaylist": True,
    "extractor_retries": 3,
    "retries": 3,
    # 유튜브가 특정 클라이언트 차단/DRM/403 걸 때 우회에 도움 되는 경우가 많음
    # (환경에 따라 web/android 조합이 더 잘 될 때가 있음)
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
}

if YTDLP_COOKIES_PATH:
    YDL_OPTS["cookiefile"] = YTDLP_COOKIES_PATH

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

def _pick_audio_url(info: dict):
    """
    yt-dlp 결과에서 실제 오디오 스트림 URL을 최대한 안전하게 고름.
    info['url']이 없거나 formats만 있는 경우 대응.
    """
    if not info:
        return None

    # 플레이리스트/검색 결과 entries
    if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
        info = info["entries"][0]

    if isinstance(info, dict) and info.get("url"):
        return info["url"]

    fmts = info.get("formats") if isinstance(info, dict) else None
    if not fmts:
        return None

    # audio-only 우선
    audio_only = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") != "none" and f.get("url")]
    if audio_only:
        # abr(오디오 비트레이트) 높은 것 우선
        audio_only.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return audio_only[0].get("url")

    # fallback: url 있는 것 중 하나
    any_url = [f for f in fmts if f.get("url")]
    if any_url:
        return any_url[0].get("url")

    return None

async def play_youtube(guild, team, url, start, duration, order=None, channel=None, preferred_member: discord.Member = None):
    vc = await connect_voice_by_guild(guild, channel, preferred_member=preferred_member)
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = _pick_audio_url(info)

        if not audio_url:
            if channel:
                await channel.send("❌ 유튜브 오디오 URL 추출 실패 (formats/url 없음)")
            return

    except Exception as e:
        if channel:
            await channel.send(f"❌ 유튜브 추출 실패: {e}")
        print(f"[yt-dlp error] {e}")
        return

    try:
        source = discord.FFmpegPCMAudio(
            audio_url,
            executable="ffmpeg",
            before_options=f"{FFMPEG_BEFORE} -ss {start}",
            options=f"-t {duration} -vn"
        )
        vc.play(
            discord.PCMVolumeTransformer(
                source,
                volume=get_volume(team)
            )
        )
    except Exception as e:
        if channel:
            await channel.send(f"❌ ffmpeg 재생 실패: {e}")
        print(f"[ffmpeg error] {e}")
        return

    if order is not None:
        try:
            game_state(team).update({"currentOrder": order})
        except Exception as e:
            print(f"[firestore game_state update error] {e}")

    if channel:
        await update_now_playing_embed(channel, guild.id, "🎶 재생 중", "유튜브 등장곡")

async def play_song(guild, team, name, order, channel, preferred_member: discord.Member = None):
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        if channel:
            await channel.send(f"❌ 등장곡 없음: {name}")
        return
    d = doc.to_dict()
    await play_youtube(
        guild, team,
        d["url"], d["start"], d["end"] - d["start"],
        order, channel, preferred_member=preferred_member
    )

# =======================
# 파일 사운드
# =======================
async def play_local_sound(guild, team, folder, channel, preferred_member: discord.Member = None):
    base = os.path.join("sounds", folder)
    if not os.path.exists(base):
        if channel:
            await channel.send(f"❌ 로컬 사운드 폴더 없음: {base}")
        return

    files = [f for f in os.listdir(base) if f.lower().endswith((".mp3", ".wav", ".ogg"))]
    if not files:
        if channel:
            await channel.send(f"❌ 로컬 사운드 파일 없음: {base}")
        return

    filename = random.choice(files)
    path = os.path.join(base, filename)

    vc = await connect_voice_by_guild(guild, channel, preferred_member=preferred_member)
    if vc is None:
        return
    if vc.is_playing():
        vc.stop()

    try:
        vc.play(
            discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(path),
                volume=get_volume(team)
            )
        )
    except Exception as e:
        if channel:
            await channel.send(f"❌ 로컬 사운드 재생 실패: {e}")
        print(f"[local sound error] {e}")
        return

    await update_now_playing_embed(
        channel, guild.id, "🎶 재생 중", f"{folder}/{filename}"
    )

# =======================
# 자동 퇴장
# =======================
async def start_idle_countdown(guild, channel):
    for remaining in range(VOICE_IDLE_SECONDS, 0, -1):
        vc = guild.voice_client
        if not vc:
            return

        # 사람이 들어오면 중단
        humans = [m for m in vc.channel.members if not m.bot]
        if humans:
            return

        await update_now_playing_embed(
            channel, guild.id, "⏸ 대기 중",
            "음성 채널에 사람이 없습니다",
            countdown=remaining
        )
        await asyncio.sleep(1)

    # 끝까지 사람이 없으면 퇴장
    if guild.voice_client:
        try:
            await guild.voice_client.disconnect()
        except Exception as e:
            print(f"[disconnect error] {e}")

# =======================
# 이벤트
# =======================
@bot.event
async def on_ready():
    print("🔥 Railway 등장곡 봇 실행 완료")
    # 혹시 View가 재시작 후에도 살아있게 하려면 add_view 가능 (timeout=None일 때)
    try:
        bot.add_view(LineupView())
    except Exception:
        pass

@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client
    channel = get_default_text_channel(guild)

    # 사람이 다 나가면 카운트다운 시작
    if vc and before.channel == vc.channel:
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans and guild.id not in idle_countdown_tasks:
            if channel:
                idle_countdown_tasks[guild.id] = asyncio.create_task(
                    start_idle_countdown(guild, channel)
                )

    # 사람이 들어오면 카운트다운 취소
    if after.channel and vc and after.channel == vc.channel:
        task = idle_countdown_tasks.pop(guild.id, None)
        if task:
            task.cancel()

    # 등장 시 자동 등장곡
    if before.channel is None and after.channel is not None:
        if not has_entrance_role(member):
            return

        nickname = member.display_name

        try:
            for team_doc in db.collection("teams").stream():
                team = team_doc.id
                for d in team_ref(team, "lineup").stream():
                    if d.to_dict().get("name") == nickname:
                        await play_song(
                            member.guild, team,
                            nickname, int(d.id),
                            channel,
                            preferred_member=member
                        )
                        return
        except Exception as e:
            print(f"[on_voice_state_update firestore error] {e}")

# =======================
# 명령어
# =======================
@bot.command(name="입장")
async def join(ctx):
    await connect_voice_by_guild(ctx.guild, ctx.channel, preferred_member=ctx.author)

@bot.command(name="퇴장")
async def leave(ctx):
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
        await ctx.send("🔇 음성 채널 퇴장")

@bot.command(name="팀")
async def set_team(ctx, team: str):
    current_team[ctx.guild.id] = team
    await ctx.send(f"✅ 현재 팀: {team}")

@bot.command(name="볼륨")
async def volume(ctx, value: int):
    if not can_manage(ctx):
        return
    if 0 <= value <= 100:
        set_volume(get_team(ctx.guild.id), value / 100)
        await ctx.send(f"🔊 볼륨 {value}%")

def parse_song(args):
    name, url, tr = [x.strip() for x in args.split(" / ", 2)]
    a, b = tr.replace("-", "~").split("~")

    def sec(t):
        if ":" in t:
            m, s = t.split(":")
            return int(m) * 60 + int(s)
        return int(t)

    return name, url.split("&")[0], sec(a), sec(b)

@bot.command(name="저장")
async def save(ctx, *, args):
    if not can_manage(ctx):
        return
    team = get_team(ctx.guild.id)
    n, u, s, e = parse_song(args)
    team_ref(team, "entranceSongs").document(n).set({"url": u, "start": s, "end": e})
    await ctx.send(f"✅ 등장곡 저장: {n}")

@bot.command(name="변경")
async def change(ctx, *, args):
    if not can_manage(ctx):
        return
    team = get_team(ctx.guild.id)
    n, u, s, e = parse_song(args)
    team_ref(team, "entranceSongs").document(n).set(
        {"url": u, "start": s, "end": e}, merge=True
    )
    await ctx.send(f"♻️ 등장곡 변경: {n}")

@bot.command(name="미리듣기")
async def preview(ctx, name: str):
    if not can_manage(ctx):
        return
    team = get_team(ctx.guild.id)
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        await ctx.send("❌ 등장곡 없음")
        return
    d = doc.to_dict()
    await play_youtube(ctx.guild, team, d["url"], d["start"], 5, None, ctx.channel, preferred_member=ctx.author)

@bot.command(name="타순")
async def set_order(ctx, num: int, *, args):
    if not can_manage(ctx):
        return
    team = get_team(ctx.guild.id)
    _, name = args.split("/", 1)
    team_ref(team, "lineup").document(str(num)).set({"name": name.strip()})
    await ctx.send(f"✅ {num}번 타순: {name.strip()}")
    await refresh_lineup(ctx)

@bot.command(name="교체")
async def change_player(ctx, num: int, *, args):
    if not can_manage(ctx):
        return
    team = get_team(ctx.guild.id)
    _, new = args.split("/", 1)
    team_ref(team, "lineup").document(str(num)).set({"name": new.strip()})
    await ctx.send(f"🔄 {num}번 교체: {new.strip()}")
    await refresh_lineup(ctx)

@bot.command(name="이벤트저장")
async def save_event(ctx, key: str, filename: str):
    if not can_manage(ctx):
        return
    team_ref(get_team(ctx.guild.id), "events").document(key).set({"file": filename})
    await ctx.send(f"✅ 이벤트 저장: {key}")

@bot.command(name="등장곡역할주기")
async def give_role(ctx, member: discord.Member):
    if not can_manage(ctx):
        return
    role = await get_or_create_role(ctx.guild)
    await member.add_roles(role)
    await ctx.send(f"🎧 역할 부여: {member.display_name}")

@bot.command(name="등장곡역할회수")
async def remove_role(ctx, member: discord.Member):
    if not can_manage(ctx):
        return
    role = await get_or_create_role(ctx.guild)
    await member.remove_roles(role)
    await ctx.send(f"❌ 역할 회수: {member.display_name}")

# =======================
# UI
# =======================
class Control(discord.ui.Button):
    def __init__(self, label, action):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        team = get_team(interaction.guild.id)
        ch = interaction.channel

        if self.action == "stop":
            if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
                interaction.guild.voice_client.stop()
                await update_now_playing_embed(
                    ch, interaction.guild.id,
                    "⏹ 정지됨", "재생 중지"
                )
            return

        if self.action == "next":
            # 현재 타순 +1로 이동(9 넘어가면 1)
            st = game_state(team).get().to_dict()
            cur = int(st.get("currentOrder", 1))
            nxt = cur + 1
            if nxt > 9:
                nxt = 1

            doc = team_ref(team, "lineup").document(str(nxt)).get()
            if doc.exists:
                name = doc.to_dict().get("name")
                if name:
                    await play_song(
                        interaction.guild, team,
                        name, nxt, ch,
                        preferred_member=interaction.user if isinstance(interaction.user, discord.Member) else None
                    )
            await refresh_lineup(interaction)
            return

        if self.action.startswith("num"):
            order = int(self.action.replace("num", ""))
            doc = team_ref(team, "lineup").document(str(order)).get()
            if doc.exists:
                name = doc.to_dict().get("name")
                if name:
                    await play_song(
                        interaction.guild, team,
                        name, order, ch,
                        preferred_member=interaction.user if isinstance(interaction.user, discord.Member) else None
                    )
                await refresh_lineup(interaction)
            return

        # 로컬 효과음
        await play_local_sound(
            interaction.guild, team, self.action, ch,
            preferred_member=interaction.user if isinstance(interaction.user, discord.Member) else None
        )

class LineupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}"))
        self.add_item(Control("다음 타자", "next"))
        self.add_item(Control("📋 라인업 송", "lineup"))
        self.add_item(Control("💥 홈런", "homerun"))
        # self.add_item(Control("💥 홈런2", "homerun2"))
        self.add_item(Control("❌ 삼진", "strikeout"))
        self.add_item(Control("⚾ 볼넷", "4ball"))
        self.add_item(Control("❗ 풀카운트", "fullcount"))
        self.add_item(Control("🤬 견제", "look"))
        self.add_item(Control("🐦‍🔥 플라이", "fly"))
        self.add_item(Control("🧤 아웃", "out"))
        self.add_item(Control("🤓 도루성공", "steal"))
        # self.add_item(Control("⚾ 득점", "score"))
        self.add_item(Control("🔁 이닝교대", "inning_change"))
        # self.add_item(Control("🛑 경기종료", "game_end"))
        self.add_item(Control("🛑 경기종료", "game_end1"))
        self.add_item(Control("⏹ 정지", "stop"))

# =======================
# 라인업
# =======================
async def build_lineup(ctx):
    team = get_team(ctx.guild.id)
    st = game_state(team).get().to_dict()
    cur = st.get("currentOrder", 1)
    embed = discord.Embed(title=f"⚾ {team} 라인업 (현재 타순: {cur}번)")
    for i in range(1, 10):
        d = team_ref(team, "lineup").document(str(i)).get()
        embed.add_field(
            name=f"{i}번",
            value=d.to_dict()["name"] if d.exists else "-",
            inline=True
        )
    return embed

async def refresh_lineup(ctx):
    gid = ctx.guild.id
    if gid in lineup_message:
        await lineup_message[gid].edit(
            embed=await build_lineup(ctx),
            view=LineupView()
        )

@bot.command(name="라인업")
async def lineup(ctx):
    lineup_message[ctx.guild.id] = await ctx.send(
        embed=await build_lineup(ctx),
        view=LineupView()
    )

@bot.command(name="도움")
async def help_cmd(ctx):
    await ctx.send(
        "!입장 / !퇴장\n"
        "!팀 팀명\n"
        "!볼륨 0~100\n"
        "!저장 이름 / URL / 시작-끝\n"
        "!변경 이름 / URL / 시작-끝\n"
        "!미리듣기 이름\n"
        "!타순 번호 / 닉네임\n"
        "!교체 번호 / 닉네임\n"
        "!이벤트저장 키 파일명\n"
        "!등장곡역할주기 @유저\n"
        "!등장곡역할회수 @유저\n"
        "!라인업\n"
        "※ 관리자 / 등장곡 재생인 / OWNER_ID 가능"
    )

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 비어있습니다.")

bot.run(TOKEN)
