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
team_voice_clients = {}

ENTRANCE_ROLE_NAME = "등장곡 재생인"

# =======================
# Firestore 구조
# entranceSongs (공용)
# teams/{team}/lineup
# =======================
def entrance_song_ref(name):
    return db.collection("entranceSongs").document(name)

def team_lineup_ref(team):
    return db.collection("teams").document(team).collection("lineup")

def team_state_ref(team):
    return db.collection("teams").document(team).collection("state")

# =======================
# 권한
# =======================
def is_admin(ctx):
    return ctx.author.guild_permissions.administrator

def has_role(member):
    return any(r.name == ENTRANCE_ROLE_NAME for r in member.roles)

def can_manage(ctx):
    return ctx.author.id == OWNER_ID or is_admin(ctx) or has_role(ctx.author)

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

# =======================
# 음성 연결 (팀별)
# =======================
async def connect_team_voice(guild, team, channel=None):
    if team in team_voice_clients and team_voice_clients[team].is_connected():
        return team_voice_clients[team]

    for m in guild.members:
        if m.voice:
            vc = await m.voice.channel.connect()
            team_voice_clients[team] = vc
            if channel:
                await channel.send(f"🔊 {team} 음성 연결: {m.voice.channel.name}")
            return vc

    if channel:
        await channel.send(f"❌ {team} 음성 연결 실패")
    return None

# =======================
# 볼륨
# =======================
def get_volume(team):
    ref = team_state_ref(team).document("volume")
    doc = ref.get()
    if not doc.exists:
        ref.set({"value": 0.5})
        return 0.5
    return doc.to_dict().get("value", 0.5)

def set_volume(team, value):
    team_state_ref(team).document("volume").set({"value": value})

# =======================
# 타순 상태
# =======================
def game_state(team):
    ref = team_state_ref(team).document("game")
    if not ref.get().exists:
        ref.set({"currentOrder": 1})
    return ref

# =======================
# Embed
# =======================
async def update_embed(channel, team, title, song, countdown=None):
    vol = int(get_volume(team) * 100)
    order = game_state(team).get().to_dict().get("currentOrder", "-")

    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="🎵 현재 곡", value=song, inline=False)
    embed.add_field(name="🏷 팀", value=team, inline=True)
    embed.add_field(name="🔊 볼륨", value=f"{vol}%", inline=True)
    embed.add_field(name="⚾ 타순", value=f"{order}번", inline=True)

    if countdown is not None:
        embed.set_footer(text=f"⏱ 자동 퇴장까지 {countdown}초")

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
    "nocheckcertificate": True
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

async def play_youtube(guild, team, url, start, duration, order, channel):
    vc = await connect_team_voice(guild, team, channel)
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info["url"]

    vc.play(discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            audio_url,
            before_options=f"{FFMPEG_BEFORE} -ss {start}",
            options=f"-t {duration} -vn"
        ),
        volume=get_volume(team)
    ))

    game_state(team).update({"currentOrder": order})
    await update_embed(channel, team, "🎶 재생 중", "등장곡")

async def play_song(guild, team, name, order, channel):
    doc = entrance_song_ref(name).get()
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
# 로컬 사운드
# =======================
async def play_local(guild, team, folder, channel):
    base = os.path.join("sounds", folder)
    if not os.path.exists(base):
        return
    files = [f for f in os.listdir(base) if f.endswith(".mp3")]
    if not files:
        return
    f = random.choice(files)

    vc = await connect_team_voice(guild, team, channel)
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    vc.play(discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(os.path.join(base, f)),
        volume=get_volume(team)
    ))

    await update_embed(channel, team, "🎶 재생 중", f"{folder}/{f}")

# =======================
# 명령어
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

    entrance_song_ref(name).set({
        "url": url.split("&")[0],
        "start": sec(a),
        "end": sec(b)
    })
    await ctx.send(f"✅ 등장곡 저장: {name}")

@bot.command(name="타순")
async def set_order(ctx, team: str, num: int, *, name):
    if not can_manage(ctx):
        return
    team_lineup_ref(team).document(str(num)).set({"name": name})
    await ctx.send(f"✅ {team} {num}번: {name}")

@bot.command(name="교체")
async def change(ctx, team: str, num: int, *, name):
    if not can_manage(ctx):
        return
    team_lineup_ref(team).document(str(num)).set({"name": name})
    await ctx.send(f"🔄 {team} {num}번 교체: {name}")

@bot.command(name="라인업")
async def lineup(ctx, team: str):
    embed = discord.Embed(title=f"⚾ {team} 라인업")
    for i in range(1, 10):
        doc = team_lineup_ref(team).document(str(i)).get()
        embed.add_field(
            name=f"{i}번",
            value=doc.to_dict()["name"] if doc.exists else "-",
            inline=True
        )
    await ctx.send(embed=embed)

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
# 자동 등장곡
# =======================
@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is None and after.channel is not None:
        if not has_role(member):
            return
        name = member.display_name
        for team_doc in db.collection("teams").stream():
            team = team_doc.id
            for d in team_lineup_ref(team).stream():
                if d.to_dict().get("name") == name:
                    await play_song(
                        member.guild,
                        team,
                        name,
                        int(d.id),
                        member.guild.text_channels[0]
                    )
                    return

@bot.event
async def on_ready():
    print("🔥 A팀 / B팀 동시 음성 등장곡 봇 실행 완료")

bot.run(TOKEN)
