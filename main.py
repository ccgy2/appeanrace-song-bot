import discord
from discord.ext import commands
import yt_dlp
import os
import json
import random
import asyncio
import base64
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

# ✅ (추가) 쿠키 환경변수 방식
YTDLP_COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
YTDLP_COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "").strip()  # 파일 경로로 쓰고 싶으면 이걸로도 가능

# =======================
# 자동 퇴장 설정값
# =======================
VOICE_IDLE_SECONDS = 300
WARNING_SECONDS = 30

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
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if perms.send_messages:
            return ch
    return None

async def connect_voice_to_channel(guild, voice_channel, text_channel=None):
    if voice_channel is None:
        if text_channel:
            await text_channel.send("❌ 연결할 음성 채널이 없습니다.")
        return None

    vc = guild.voice_client

    if vc:
        if vc.channel.id == voice_channel.id:
            return vc
        await vc.move_to(voice_channel)
        if text_channel:
            await text_channel.send(f"🔊 음성 채널 이동: {voice_channel.name}")
        return vc

    vc = await voice_channel.connect()
    if text_channel:
        await text_channel.send(f"🔊 음성 채널 연결: {voice_channel.name}")
    return vc

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
# ✅ 쿠키 준비 (B64 -> /tmp 파일)
# =======================
def prepare_ytdlp_cookies_file():
    """
    1) YTDLP_COOKIES_PATH가 있으면 그걸 사용
    2) 없고 YTDLP_COOKIES_B64가 있으면 /tmp/ytdlp_cookies.txt 생성해서 사용
    """
    if YTDLP_COOKIES_PATH:
        if os.path.exists(YTDLP_COOKIES_PATH):
            return YTDLP_COOKIES_PATH
        print(f"[cookies] YTDLP_COOKIES_PATH 지정됐지만 파일이 없음: {YTDLP_COOKIES_PATH}")

    if not YTDLP_COOKIES_B64:
        return None

    try:
        raw = base64.b64decode(YTDLP_COOKIES_B64.encode("utf-8"))
        path = "/tmp/ytdlp_cookies.txt"
        with open(path, "wb") as f:
            f.write(raw)
        print("[cookies] 쿠키 파일 생성 완료: /tmp/ytdlp_cookies.txt")
        return path
    except Exception as e:
        print(f"[cookies] 쿠키 파일 생성 실패: {e}")
        return None

COOKIES_FILE = prepare_ytdlp_cookies_file()

# =======================
# 오디오 (유튜브)
# =======================
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
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
}

if COOKIES_FILE:
    YDL_OPTS["cookiefile"] = COOKIES_FILE

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

def _pick_audio_url(info: dict):
    if not info:
        return None

    if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
        info = info["entries"][0]

    if isinstance(info, dict) and info.get("url"):
        return info["url"]

    fmts = info.get("formats") if isinstance(info, dict) else None
    if not fmts:
        return None

    audio_only = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") != "none" and f.get("url")]
    if audio_only:
        audio_only.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return audio_only[0].get("url")

    any_url = [f for f in fmts if f.get("url")]
    if any_url:
        return any_url[0].get("url")

    return None

async def play_youtube(guild, team, url, start, duration, order=None, channel=None):
    vc = await connect_voice_by_guild(guild, channel)
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
        msg = f"❌ 유튜브 추출 실패: {e}"
        print(msg)
        if channel:
            await channel.send(msg)
            if not COOKIES_FILE:
                await channel.send("⚠️ 현재 쿠키가 적용되지 않았습니다. (YTDLP_COOKIES_B64 또는 YTDLP_COOKIES_PATH 필요)")
        return

    try:
        vc.play(
            discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    audio_url,
                    executable="ffmpeg",
                    before_options=f"{FFMPEG_BEFORE} -ss {start}",
                    options=f"-t {duration} -vn"
                ),
                volume=get_volume(team)
            )
        )
    except Exception as e:
        msg = f"❌ ffmpeg 재생 실패: {e}"
        print(msg)
        if channel:
            await channel.send(msg)
        return

    if order is not None:
        game_state(team).update({"currentOrder": order})

    if channel:
        await update_now_playing_embed(channel, guild.id, "🎶 재생 중", "유튜브 등장곡")

async def play_song(guild, team, name, order, channel):
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        await channel.send(f"❌ 등장곡 없음: {name}")
        return
    d = doc.to_dict()
    await play_youtube(
        guild, team,
        d["url"], d["start"], d["end"] - d["start"],
        order, channel
    )

# =======================
# 파일 사운드
# =======================
async def play_local_sound(guild, team, folder, channel):
    base = os.path.join("sounds", folder)
    if not os.path.exists(base):
        return
    files = [f for f in os.listdir(base) if f.lower().endswith((".mp3", ".wav", ".ogg"))]
    if not files:
        return
    filename = random.choice(files)
    path = os.path.join(base, filename)

    vc = await connect_voice_by_guild(guild, channel)
    if vc is None:
        return
    if vc.is_playing():
        vc.stop()

    vc.play(discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(path),
        volume=get_volume(team)
    ))

    await update_now_playing_embed(
        channel, guild.id, "🎶 재생 중", f"{folder}/{filename}"
    )

# =======================
# 자동 퇴장
# =======================
async def start_idle_countdown(guild, channel):
    for remaining in range(VOICE_IDLE_SECONDS, 0, -1):
        vc = guild.voice_client
        if not vc or [m for m in vc.channel.members if not m.bot]:
            return
        await update_now_playing_embed(
            channel, guild.id, "⏸ 대기 중",
            "음성 채널에 사람이 없습니다",
            countdown=remaining
        )
        await asyncio.sleep(1)
    if guild.voice_client:
        await guild.voice_client.disconnect()

# =======================
# ✅ 이름 변경 (데이터 손실 없이 문서ID/필드만 변경)
# =======================
def rename_player_in_team(team: str, old_name: str, new_name: str):
    """
    - entranceSongs/{old_name} -> entranceSongs/{new_name}로 복사 후 old 삭제 (데이터 유지)
    - lineup/{1~9} 문서들의 name 값에서 old_name -> new_name 변경
    """
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()

    if not old_name or not new_name:
        return False, "이름이 비어있습니다."
    if old_name == new_name:
        return False, "기존 이름과 새 이름이 같습니다."

    # 1) entranceSongs 문서 rename
    songs_col = team_ref(team, "entranceSongs")
    old_doc_ref = songs_col.document(old_name)
    new_doc_ref = songs_col.document(new_name)

    old_doc = old_doc_ref.get()
    new_doc = new_doc_ref.get()

    # 새 문서가 이미 있으면 덮어쓰기 위험 -> 막음
    if new_doc.exists:
        return False, f"새 이름({new_name})의 등장곡 문서가 이미 존재합니다. (중복)"

    # old가 없을 수도 있음(등장곡 저장 안 했거나 다른 케이스). 그럼 lineup만 처리
    batch = db.batch()

    if old_doc.exists:
        data = old_doc.to_dict()
        batch.set(new_doc_ref, data)
        batch.delete(old_doc_ref)

    # 2) lineup name 치환
    lineup_col = team_ref(team, "lineup")
    changed_lineup = 0
    for i in range(1, 10):
        ref = lineup_col.document(str(i))
        doc = ref.get()
        if doc.exists:
            d = doc.to_dict() or {}
            if d.get("name") == old_name:
                batch.set(ref, {"name": new_name}, merge=True)
                changed_lineup += 1

    batch.commit()

    info = []
    if old_doc.exists:
        info.append("등장곡 문서 이름 변경 완료")
    else:
        info.append("등장곡 문서가 없어서(미저장) 스킵")

    info.append(f"라인업 변경 {changed_lineup}개")
    return True, " / ".join(info)

# =======================
# 이벤트
# =======================
@bot.event
async def on_ready():
    print("🔥 Railway 등장곡 봇 실행 완료")
    if COOKIES_FILE:
        print("[cookies] yt-dlp 쿠키 적용됨 ✅")
    else:
        print("[cookies] yt-dlp 쿠키 미적용 ⚠️ (차단되면 YTDLP_COOKIES_B64 필요)")

@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client
    channel = get_default_text_channel(guild)

    if vc and before.channel == vc.channel:
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans and guild.id not in idle_countdown_tasks and channel:
            idle_countdown_tasks[guild.id] = asyncio.create_task(
                start_idle_countdown(guild, channel)
            )

    if after.channel and vc and after.channel == vc.channel:
        task = idle_countdown_tasks.pop(guild.id, None)
        if task:
            task.cancel()

    if before.channel is None and after.channel is not None:
        if not has_entrance_role(member):
            return
        nickname = member.display_name
        for team_doc in db.collection("teams").stream():
            team = team_doc.id
            for d in team_ref(team, "lineup").stream():
                if d.to_dict().get("name") == nickname:
                    if channel:
                        await play_song(
                            member.guild, team,
                            nickname, int(d.id),
                            channel
                        )
                    return

# =======================
# 명령어
# =======================
@bot.command(name="입장")
async def join(ctx):
    await connect_voice_by_guild(ctx.guild, ctx.channel)

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
    await play_youtube(ctx.guild, team, d["url"], d["start"], 5, None, ctx.channel)

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
# ✅ 새 명령어: 이름변경
# 사용법:
# 1) !이름변경 새닉네임            -> 본인 이름(현재 display_name 기준) 변경
# 2) !이름변경 기존닉네임 / 새닉네임 -> (관리자/권한자) 특정 이름 변경
# =======================
@bot.command(name="이름변경")
async def rename_name(ctx, *, args: str):
    team = get_team(ctx.guild.id)

    args = (args or "").strip()
    if not args:
        await ctx.send("❌ 사용법: `!이름변경 새닉네임` 또는 `!이름변경 기존닉 / 새닉`")
        return

    # 2개 인자(기존/새) 형태면 관리자만 허용
    if " / " in args:
        if not can_manage(ctx):
            await ctx.send("❌ 권한이 없습니다. (관리자/등장곡 재생인/OWNER_ID만 가능)")
            return
        try:
            old_name, new_name = [x.strip() for x in args.split(" / ", 1)]
        except Exception:
            await ctx.send("❌ 사용법: `!이름변경 기존닉 / 새닉`")
            return
    else:
        # 1개 인자면 본인 이름 변경 (권한 없어도 가능)
        old_name = ctx.author.display_name
        new_name = args.strip()

    ok, msg = rename_player_in_team(team, old_name, new_name)
    if ok:
        await ctx.send(f"✅ 이름변경 완료: **{old_name}** → **{new_name}**\n({msg})")
        await refresh_lineup(ctx)
    else:
        await ctx.send(f"❌ 이름변경 실패: {msg}")

# =======================
# UI
# =======================
class Control(discord.ui.Button):
    def __init__(self, label, action):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.action = action

    async def callback(self, interaction):
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

        if self.action.startswith("num"):
            order = int(self.action.replace("num", ""))
            doc = team_ref(team, "lineup").document(str(order)).get()
            if doc.exists:
                await play_song(
                    interaction.guild, team,
                    doc.to_dict().get("name", ""), order, ch
                )
                await refresh_lineup(interaction)
        else:
            await play_local_sound(interaction.guild, team, self.action, ch)

class LineupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}"))
        self.add_item(Control("다음 타자", "next"))
        self.add_item(Control("📋 라인업 송", "lineup"))
        self.add_item(Control("💥 홈런", "homerun"))
        self.add_item(Control("❌ 삼진", "strikeout"))
        self.add_item(Control("⚾ 볼넷", "4ball"))
        self.add_item(Control("❗ 풀카운트", "fullcount"))
        self.add_item(Control("🤬 견제", "look"))
        self.add_item(Control("🐦‍🔥 플라이", "fly"))
        self.add_item(Control("🧤 아웃", "out"))
        self.add_item(Control("🤓 도루성공", "steal"))
        self.add_item(Control("🔁 이닝교대", "inning_change"))
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
            value=d.to_dict().get("name") if d.exists else "-",
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
        "!이름변경 새닉네임\n"
        "!이름변경 기존닉 / 새닉\n"
        "!라인업\n"
        "※ 관리자 / 등장곡 재생인 / OWNER_ID 가능"
    )

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 비어있습니다.")

bot.run(TOKEN)

