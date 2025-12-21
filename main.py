import discord
from discord.ext import commands
import yt_dlp
import os
import json
import random
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

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

async def connect_voice_by_guild(guild, log_channel=None):
    vc = guild.voice_client
    if vc:
        return vc
    for m in guild.members:
        if m.voice:
            vc = await m.voice.channel.connect()
            if log_channel:
                await log_channel.send(f"🔊 음성 채널 연결됨: {m.voice.channel.name}")
            return vc
    if log_channel:
        await log_channel.send("❌ 음성 채널에 아무도 없음")
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
# 오디오 (유튜브)
# =======================
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "socket_timeout": 10,
    "source_address": "0.0.0.0"
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

async def play_youtube(guild, team, url, start, duration, order=None, log_channel=None):
    vc = await connect_voice_by_guild(guild, log_channel)
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info["url"]

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(
            audio_url,
            executable="ffmpeg",
            before_options=f"{FFMPEG_BEFORE} -ss {start}",
            options=f"-t {duration} -vn"
        ),
        volume=get_volume(team)
    )

    vc.play(source)

    if log_channel:
        await log_channel.send("▶️ 노래 재생")

    if order is not None:
        game_state(team).update({"currentOrder": order})

async def play_song(guild, team, name, order, log_channel):
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        await log_channel.send(f"❌ 등장곡 없음: {name}")
        return
    d = doc.to_dict()
    await play_youtube(
        guild, team,
        d["url"],
        d["start"],
        d["end"] - d["start"],
        order,
        log_channel
    )

# =======================
# 파일 기반 사운드 (이벤트/라인업)
# =======================
async def play_local_sound(guild, team, folder, log_channel):
    base = os.path.join("sounds", folder)
    if not os.path.exists(base):
        await log_channel.send(f"❌ 폴더 없음: sounds/{folder}")
        return

    files = [
        f for f in os.listdir(base)
        if f.lower().endswith((".mp3", ".wav", ".ogg"))
    ]
    if not files:
        await log_channel.send(f"❌ sounds/{folder} 안에 파일 없음")
        return

    filename = random.choice(files)
    path = os.path.join(base, filename)

    vc = await connect_voice_by_guild(guild, log_channel)
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    vc.play(
        discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(path),
            volume=get_volume(team)
        )
    )

    await log_channel.send(f"🎵 재생됨: {folder}/{filename}")

# =======================
# READY
# =======================
@bot.event
async def on_ready():
    print("🔥 Railway 등장곡 봇 실행 완료")

# =======================
# 자동 등장곡 (음성 입장)
# =======================
@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is None and after.channel is not None:
        if not has_entrance_role(member):
            return
        nickname = member.display_name
        for team_doc in db.collection("teams").stream():
            team = team_doc.id
            for d in team_ref(team, "lineup").stream():
                if d.to_dict().get("name") == nickname:
                    await play_song(
                        member.guild,
                        team,
                        nickname,
                        int(d.id),
                        member.guild.text_channels[0]
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
    if not is_admin(ctx):
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
    if not is_admin(ctx):
        return
    team = get_team(ctx.guild.id)
    n, u, s, e = parse_song(args)
    team_ref(team, "entranceSongs").document(n).set({
        "url": u, "start": s, "end": e
    })
    await ctx.send(f"✅ 등장곡 저장: {n}")

@bot.command(name="타순")
async def set_order(ctx, num: int, *, args):
    if not is_admin(ctx):
        return
    team = get_team(ctx.guild.id)
    _, name = args.split("/", 1)
    team_ref(team, "lineup").document(str(num)).set({"name": name.strip()})
    await ctx.send(f"✅ {num}번 타순: {name.strip()}")
    await refresh_lineup(ctx)

@bot.command(name="등장곡역할주기")
async def give_role(ctx, member: discord.Member):
    if not is_admin(ctx):
        return
    role = await get_or_create_role(ctx.guild)
    await member.add_roles(role)
    await ctx.send(f"🎧 역할 부여: {member.display_name}")

@bot.command(name="등장곡역할회수")
async def remove_role(ctx, member: discord.Member):
    if not is_admin(ctx):
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

        if self.action.startswith("num"):
            order = int(self.action.replace("num", ""))
            doc = team_ref(team, "lineup").document(str(order)).get()
            if doc.exists:
                await play_song(
                    interaction.guild,
                    team,
                    doc.to_dict()["name"],
                    order,
                    ch
                )
                await refresh_lineup(interaction)

        elif self.action == "lineup_song":
            await play_local_sound(interaction.guild, team, "lineup", ch)

        elif self.action == "homerun":
            await play_local_sound(interaction.guild, team, "homerun", ch)

        elif self.action == "strikeout":
            await play_local_sound(interaction.guild, team, "strikeout", ch)

        elif self.action == "out":
            await play_local_sound(interaction.guild, team, "out", ch)

        elif self.action == "score":
            await play_local_sound(interaction.guild, team, "score", ch)

        elif self.action == "fly":
            await play_local_sound(interaction.guild, team, "fly", ch)

        elif self.action == "game_end":
            await play_local_sound(interaction.guild, team, "game_end", ch)

        elif self.action == "inning_change":
            await play_local_sound(interaction.guild, team, "inning_change", ch)

        elif self.action == "stop":
            if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
                interaction.guild.voice_client.stop()
                await ch.send("⏹ 재생 중지")

class LineupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}"))
        self.add_item(Control("📋 라인업 송", "lineup_song"))
        self.add_item(Control("💥 홈런", "homerun"))
        self.add_item(Control("❌ 삼진", "strikeout"))
        self.add_item(Control("🐦‍🔥 플라이", "fly"))
        self.add_item(Control("🧤 아웃", "out"))
        self.add_item(Control("⚾ 득점", "score"))
        self.add_item(Control("⏹ 중지", "stop"))
        self.add_item(Control("🛑 경기종료", "game_end"))
        self.add_item(Control("🔁 이닝교대", "inning_change"))

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
        "!타순 번호 / 닉네임\n"
        "!라인업\n"
        "버튼: 라인업송 / 홈런 / 삼진 / 아웃 / 득점"
    )

bot.run(TOKEN)

