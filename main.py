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
voice_idle_tasks = {}
idle_countdown_tasks = {}
now_playing_message = {}

ENTRANCE_ROLE_NAME = "등장곡 재생자"

# =======================
# 팀 / Firestore
# =======================
def get_team(guild_id):
    return current_team.get(guild_id, "A팀")

def team_ref(team, path):
    return db.collection("teams").document(team).collection(path)

# =======================
# 유틸
# =======================
def is_admin(ctx):
    return ctx.author.guild_permissions.administrator

def has_entrance_role(member):
    return any(r.name == ENTRANCE_ROLE_NAME for r in member.roles)

async def get_or_create_role(guild, log_channel=None):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        try:
            role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
            if log_channel:
                await log_channel.send(f"ℹ️ 역할 생성됨: {ENTRANCE_ROLE_NAME}")
        except Exception as e:
            if log_channel:
                await log_channel.send(f"❌ 역할 생성 실패: {e}")
            raise
    return role

async def connect_voice_by_guild(guild, channel=None):
    if guild.voice_client:
        return guild.voice_client
    for m in guild.members:
        if m.voice:
            vc = await m.voice.channel.connect()
            if channel:
                await channel.send(f"🔊 음성 채널 연결: {m.voice.channel.name}")
            return vc
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
# Embed (현재 재생 상태)
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

    if guild_id not in now_playing_message:
        now_playing_message[guild_id] = await channel.send(embed=embed)
    else:
        await now_playing_message[guild_id].edit(embed=embed)

# =======================
# 오디오 / 생략 (기존과 동일)
# =======================
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "socket_timeout": 10,
    "source_address": "0.0.0.0"
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

async def play_youtube(guild, team, url, start, duration, order=None, channel=None):
    vc = await connect_voice_by_guild(guild, channel)
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
            executable="ffmpeg",
            before_options=f"{FFMPEG_BEFORE} -ss {start}",
            options=f"-t {duration} -vn"
        ),
        volume=get_volume(team)
    ))

# =======================
# 역할 명령어 (로그 추가 핵심)
# =======================
@bot.command(name="등장곡역할주기")
async def give_role(ctx, member: discord.Member):
    log = ctx.channel
    if not is_admin(ctx):
        await log.send("❌ 관리자만 사용 가능")
        return

    try:
        role = await get_or_create_role(ctx.guild, log)
        if ctx.guild.me.top_role <= role:
            await log.send("❌ 봇 역할이 대상 역할보다 낮습니다")
            return
        if ctx.guild.me.top_role <= member.top_role:
            await log.send("❌ 대상 유저의 역할이 봇보다 높습니다")
            return

        await member.add_roles(role)
        await log.send(f"✅ 역할 부여 성공: {member.display_name}")

    except discord.Forbidden:
        await log.send("❌ 권한 부족 (역할 관리 권한 확인)")
    except Exception as e:
        await log.send(f"❌ 알 수 없는 오류: {e}")

@bot.command(name="등장곡역할회수")
async def remove_role(ctx, member: discord.Member):
    log = ctx.channel
    if not is_admin(ctx):
        await log.send("❌ 관리자만 사용 가능")
        return

    role = discord.utils.get(ctx.guild.roles, name=ENTRANCE_ROLE_NAME)
    if not role:
        await log.send("❌ 역할이 존재하지 않음")
        return

    try:
        await member.remove_roles(role)
        await log.send(f"✅ 역할 회수 성공: {member.display_name}")
    except discord.Forbidden:
        await log.send("❌ 권한 부족 (역할 관리 권한 확인)")
    except Exception as e:
        await log.send(f"❌ 알 수 없는 오류: {e}")

# =======================
# RUN
# =======================
@bot.event
async def on_ready():
    print("🔥 Railway 등장곡 봇 실행 완료")

bot.run(TOKEN)
