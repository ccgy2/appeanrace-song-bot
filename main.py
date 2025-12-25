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
# 자동 퇴장 설정
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
lineup_message = {}
idle_tasks = {}
now_playing_message = {}

ENTRANCE_ROLE_NAME = "등장곡 재생인"

# =======================
# 권한
# =======================
def has_role(member):
    return any(r.name == ENTRANCE_ROLE_NAME for r in member.roles)

def can_manage(ctx):
    return (
        ctx.author.guild_permissions.administrator
        or has_role(ctx.author)
        or ctx.author.id == OWNER_ID
    )

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

# =======================
# Firestore 구조
# entranceSongs (공용)
# teams/{team}/lineup
# teams/{team}/state
# =======================
def team_ref(team, path):
    return db.collection("teams").document(team).collection(path)

def volume_ref(team):
    return team_ref(team, "state").document("volume")

def get_volume(team):
    doc = volume_ref(team).get()
    if not doc.exists:
        volume_ref(team).set({"value": 0.5})
        return 0.5
    return doc.to_dict().get("value", 0.5)

def set_volume(team, value):
    volume_ref(team).set({"value": value})

def game_state(team):
    ref = team_ref(team, "state").document("game")
    if not ref.get().exists:
        ref.set({"currentOrder": 1})
    return ref

# =======================
# 음성 연결
# =======================
async def connect_voice(guild):
    if guild.voice_client:
        return guild.voice_client
    for m in guild.members:
        if m.voice:
            return await m.voice.channel.connect()
    return None

# =======================
# Embed
# =======================
async def update_now_playing(channel, team, title, song):
    vol = int(get_volume(team) * 100)
    order = game_state(team).get().to_dict().get("currentOrder", "-")

    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="🎵 현재 곡", value=song, inline=False)
    embed.add_field(name="🏷 팀", value=team, inline=True)
    embed.add_field(name="🔊 볼륨", value=f"{vol}%", inline=True)
    embed.add_field(name="⚾ 타순", value=f"{order}번", inline=True)

    key = f"{channel.guild.id}_{team}"
    if key not in now_playing_message:
        now_playing_message[key] = await channel.send(embed=embed)
    else:
        await now_playing_message[key].edit(embed=embed)

# =======================
# 오디오 (유튜브)
# =======================
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

async def play_youtube(guild, team, url, start, duration, order, channel):
    vc = await connect_voice(guild)
    if not vc:
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
                before_options=f"{FFMPEG_BEFORE} -ss {start}",
                options=f"-t {duration} -vn",
            ),
            volume=get_volume(team),
        )
    )

    game_state(team).update({"currentOrder": order})
    await update_now_playing(channel, team, "🎶 재생 중", "유튜브 등장곡")

async def play_song(guild, team, name, order, channel):
    doc = db.collection("entranceSongs").document(name).get()
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
    files = [f for f in os.listdir(base) if f.endswith(".mp3")]
    if not files:
        return

    path = os.path.join(base, random.choice(files))
    vc = await connect_voice(guild)
    if not vc:
        return
    if vc.is_playing():
        vc.stop()

    vc.play(
        discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(path),
            volume=get_volume(team)
        )
    )
    await update_now_playing(channel, team, "🎶 재생 중", f"{folder}")

# =======================
# UI 버튼
# =======================
class Control(discord.ui.Button):
    def __init__(self, label, action, team):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.action = action
        self.team = team

    async def callback(self, interaction):
        await interaction.response.defer()
        ch = interaction.channel
        guild = interaction.guild

        if self.action == "stop":
            if guild.voice_client:
                guild.voice_client.stop()
                await update_now_playing(ch, self.team, "⏹ 정지", "정지됨")
            return

        if self.action.startswith("num"):
            order = int(self.action.replace("num", ""))
            doc = team_ref(self.team, "lineup").document(str(order)).get()
            if doc.exists:
                await play_song(
                    guild, self.team,
                    doc.to_dict()["name"], order, ch
                )
        else:
            await play_local_sound(guild, self.team, self.action, ch)

class LineupView(discord.ui.View):
    def __init__(self, team):
        super().__init__(timeout=None)
        self.team = team
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}", team))
        self.add_item(Control("⏹ 정지", "stop", team))
        self.add_item(Control("📋 라인업", "lineup", team))
        self.add_item(Control("💥 홈런", "homerun", team))
        self.add_item(Control("❌ 삼진", "strikeout", team))
        self.add_item(Control("🐦 플라이", "fly", team))
        self.add_item(Control("🧤 아웃", "out", team))
        self.add_item(Control("⚾ 득점", "score", team))
        self.add_item(Control("🔁 이닝교대", "inning_change", team))
        self.add_item(Control("🛑 종료", "game_end", team))

# =======================
# 라인업
# =======================
async def build_lineup(team):
    st = game_state(team).get().to_dict()
    cur = st.get("currentOrder", 1)
    embed = discord.Embed(title=f"⚾ {team} 라인업 (현재 {cur}번)")
    for i in range(1, 10):
        d = team_ref(team, "lineup").document(str(i)).get()
        embed.add_field(
            name=f"{i}번",
            value=d.to_dict()["name"] if d.exists else "-",
            inline=True
        )
    return embed

@bot.command(name="라인업")
async def lineup(ctx, team: str):
    embed = await build_lineup(team)
    lineup_message[f"{ctx.guild.id}_{team}"] = await ctx.send(
        embed=embed,
        view=LineupView(team)
    )

# =======================
# 명령어 (관리)
# =======================
@bot.command(name="저장")
async def save(ctx, *, args):
    if not can_manage(ctx):
        return
    name, url, tr = [x.strip() for x in args.split(" / ", 2)]
    a, b = tr.split("-")
    def sec(t):
        if ":" in t:
            m, s = t.split(":")
            return int(m) * 60 + int(s)
        return int(t)
    db.collection("entranceSongs").document(name).set({
        "url": url.split("&")[0],
        "start": sec(a),
        "end": sec(b)
    })
    await ctx.send(f"✅ 저장 완료: {name}")

@bot.command(name="타순")
async def set_order(ctx, team: str, num: int, *, name):
    if not can_manage(ctx):
        return
    team_ref(team, "lineup").document(str(num)).set({"name": name})
    await ctx.send(f"✅ {team} {num}번: {name}")

@bot.command(name="교체")
async def change(ctx, team: str, num: int, *, name):
    if not can_manage(ctx):
        return
    team_ref(team, "lineup").document(str(num)).set({"name": name})
    await ctx.send(f"🔄 {team} {num}번 교체: {name}")

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
# RUN
# =======================
bot.run(TOKEN)
