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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# =======================
# 자동 퇴장 설정값
# =======================
VOICE_IDLE_SECONDS = 300
WARNING_SECONDS = 30

# =======================
# Firebase
# =======================
if not firebase_admin._apps:
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
last_text_channel = {}  # 🔥 마지막 사용 텍스트 채널 저장

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
    return is_admin(ctx) or has_entrance_role(ctx.author) or ctx.author.id == OWNER_ID

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

# =======================
# 음성 연결
# =======================
async def connect_voice_by_guild(guild, voice_channel):
    if guild.voice_client:
        return guild.voice_client
    return await voice_channel.connect()

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
# Embed (🔥 텍스트 채널 고정)
# =======================
async def update_now_playing_embed(guild_id, title, song, countdown=None):
    if guild_id not in last_text_channel:
        return

    channel = last_text_channel[guild_id]
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

    if guild_id not in now_playing_message:
        now_playing_message[guild_id] = await channel.send(embed=embed)
    else:
        await now_playing_message[guild_id].edit(embed=embed)

# =======================
# 오디오
# =======================
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "socket_timeout": 10,
    "source_address": "0.0.0.0"
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

async def play_youtube(guild, team, url, start, duration, order=None):
    vc = guild.voice_client
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info["url"]

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

    if order is not None:
        game_state(team).update({"currentOrder": order})

    await update_now_playing_embed(guild.id, "🎶 재생 중", "유튜브 등장곡")

async def play_song(guild, team, name, order):
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        return
    d = doc.to_dict()
    await play_youtube(
        guild, team,
        d["url"], d["start"], d["end"] - d["start"],
        order
    )

# =======================
# 자동 퇴장
# =======================
async def start_idle_countdown(guild):
    for remaining in range(VOICE_IDLE_SECONDS, 0, -1):
        vc = guild.voice_client
        if not vc or [m for m in vc.channel.members if not m.bot]:
            return
        await update_now_playing_embed(
            guild.id,
            "⏸ 대기 중",
            "음성 채널에 사람이 없습니다",
            countdown=remaining
        )
        await asyncio.sleep(1)
    if guild.voice_client:
        await guild.voice_client.disconnect()

# =======================
# 이벤트
# =======================
@bot.event
async def on_ready():
    print("🔥 Railway 등장곡 봇 실행 완료")

@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client

    if vc and before.channel == vc.channel:
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans and guild.id not in idle_countdown_tasks:
            idle_countdown_tasks[guild.id] = asyncio.create_task(
                start_idle_countdown(guild)
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
                    await play_song(
                        member.guild, team,
                        nickname, int(d.id)
                    )
                    return

# =======================
# 명령어
# =======================
@bot.command(name="입장")
async def join(ctx):
    last_text_channel[ctx.guild.id] = ctx.channel

    if not ctx.author.voice:
        await ctx.send("❌ 먼저 음성 채널에 들어가 주세요")
        return

    if ctx.guild.voice_client:
        await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
    else:
        await ctx.author.voice.channel.connect()

    await ctx.send(f"🔊 음성 채널 입장: {ctx.author.voice.channel.name}")

@bot.command(name="퇴장")
async def leave(ctx):
    last_text_channel[ctx.guild.id] = ctx.channel
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()
        await ctx.send("🔇 음성 채널 퇴장")

@bot.command(name="팀")
async def set_team(ctx, team: str):
    last_text_channel[ctx.guild.id] = ctx.channel
    current_team[ctx.guild.id] = team
    await ctx.send(f"✅ 현재 팀: {team}")

@bot.command(name="볼륨")
async def volume(ctx, value: int):
    last_text_channel[ctx.guild.id] = ctx.channel
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

# =======================
# 🔥 이름 변경 (데이터 유지)
# =======================
@bot.command(name="이름변경")
async def rename_player(ctx, old_name: str, new_name: str):
    if not can_manage(ctx):
        return

    team = get_team(ctx.guild.id)
    changed = False

    for d in team_ref(team, "lineup").stream():
        if d.to_dict().get("name") == old_name:
            team_ref(team, "lineup").document(d.id).update({"name": new_name})
            changed = True

    old_doc = team_ref(team, "entranceSongs").document(old_name)
    if old_doc.get().exists:
        data = old_doc.get().to_dict()
        team_ref(team, "entranceSongs").document(new_name).set(data)
        old_doc.delete()
        changed = True

    if changed:
        await ctx.send(f"✅ 이름 변경 완료: {old_name} → {new_name}")
        await refresh_lineup(ctx)
    else:
        await ctx.send("❌ 해당 이름을 찾을 수 없습니다")

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
                    doc.to_dict()["name"], order, ch
                )
                await refresh_lineup(interaction)
        else:
            await play_local_sound(interaction.guild, team, self.action, ch)

class LineupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}"))
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
        "!이름변경 기존닉 새닉\n"
        "!라인업\n"
        "※ 관리자 / 등장곡 재생인 / OWNER_ID 가능"
    )

bot.run(TOKEN)

