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
# 상수 / 상태
# =======================
ENTRANCE_ROLE_NAME = "등장곡 재생인"

lineup_message = {}
idle_tasks = {}
now_playing_message = {}

# =======================
# 권한
# =======================
def has_entrance_role(member):
    return any(r.name == ENTRANCE_ROLE_NAME for r in member.roles)

def can_manage(ctx):
    return (
        ctx.author.guild_permissions.administrator
        or has_entrance_role(ctx.author)
        or ctx.author.id == OWNER_ID
    )

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

# =======================
# Firestore 구조
# =======================
# entranceSongs (공용)
# events (공용)
# teams/{team}/lineup
# teams/{team}/state (volume, game)
# teams_meta/{teamKey} -> 실제 팀명
# =======================
def team_ref(team, path):
    return db.collection("teams").document(team).collection(path)

def team_meta(team):
    return db.collection("teams_meta").document(team)

def get_team_name(team):
    doc = team_meta(team).get()
    return doc.to_dict().get("name", team) if doc.exists else team

def set_team_name(team, name):
    team_meta(team).set({"name": name})

def volume_ref(team):
    return team_ref(team, "state").document("volume")

def get_volume(team):
    doc = volume_ref(team).get()
    if not doc.exists:
        volume_ref(team).set({"value": 0.5})
        return 0.5
    return doc.to_dict()["value"]

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
    team_name = get_team_name(team)

    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="🎵 현재 곡", value=song, inline=False)
    embed.add_field(name="🏷 팀", value=team_name, inline=True)
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
YDL_OPTS = {"format": "bestaudio/best", "quiet": True}
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
        await channel.send("❌ 등장곡 없음")
        return
    d = doc.to_dict()
    await play_youtube(
        guild, team,
        d["url"], d["start"], d["end"] - d["start"],
        order, channel
    )

# =======================
# 파일 사운드 (이벤트)
# =======================
async def play_event_sound(guild, team, key, channel):
    doc = db.collection("events").document(key).get()
    if not doc.exists:
        await channel.send("❌ 이벤트 없음")
        return
    path = os.path.join("sounds", doc.to_dict()["file"])
    if not os.path.exists(path):
        await channel.send("❌ 파일 없음")
        return

    vc = await connect_voice(guild)
    if not vc:
        return
    if vc.is_playing():
        vc.stop()

    vc.play(discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(path),
        volume=get_volume(team)
    ))

    await update_now_playing(channel, team, "🎶 재생 중", key)

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
            await play_event_sound(guild, self.team, self.action, ch)

class LineupView(discord.ui.View):
    def __init__(self, team):
        super().__init__(timeout=None)
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}", team))
        self.add_item(Control("⏹ 정지", "stop", team))

# =======================
# 명령어
# =======================
@bot.command(name="입장")
async def join(ctx):
    await connect_voice(ctx.guild)

@bot.command(name="퇴장")
async def leave(ctx):
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()

@bot.command(name="팀")
async def set_team_name_cmd(ctx, team: str, *, name: str):
    if not can_manage(ctx):
        return
    set_team_name(team, name)
    await ctx.send(f"✅ {team} → {name}")

@bot.command(name="볼륨")
async def volume(ctx, team: str, value: int):
    if not can_manage(ctx):
        return
    set_volume(team, value / 100)
    await ctx.send(f"🔊 {get_team_name(team)} 볼륨 {value}%")

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
    await ctx.send(f"✅ 등장곡 저장: {name}")

@bot.command(name="변경")
async def change(ctx, *, args):
    await save(ctx, args=args)

@bot.command(name="미리듣기")
async def preview(ctx, name: str):
    if not can_manage(ctx):
        return
    doc = db.collection("entranceSongs").document(name).get()
    if not doc.exists:
        return
    d = doc.to_dict()
    await play_youtube(ctx.guild, "A팀", d["url"], d["start"], 5, 0, ctx.channel)

@bot.command(name="타순")
async def set_order(ctx, team: str, num: int, *, name: str):
    if not can_manage(ctx):
        return
    team_ref(team, "lineup").document(str(num)).set({"name": name})
    await ctx.send(f"✅ {get_team_name(team)} {num}번: {name}")

@bot.command(name="교체")
async def change_player(ctx, team: str, num: int, *, name: str):
    await set_order(ctx, team, num, name=name)

@bot.command(name="이벤트저장")
async def save_event(ctx, key: str, filename: str):
    if not can_manage(ctx):
        return
    db.collection("events").document(key).set({"file": filename})
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

@bot.command(name="라인업")
async def lineup(ctx, team: str = "A팀"):
    embed = discord.Embed(title=f"⚾ {get_team_name(team)} 라인업")
    for i in range(1, 10):
        d = team_ref(team, "lineup").document(str(i)).get()
        embed.add_field(
            name=f"{i}번",
            value=d.to_dict()["name"] if d.exists else "-",
            inline=True
        )
    await ctx.send(embed=embed, view=LineupView(team))

@bot.command(name="도움")
async def help_cmd(ctx):
    await ctx.send(
        "!입장 / !퇴장\n"
        "!팀 팀명 실제이름\n"
        "!볼륨 팀명 0~100\n"
        "!저장 이름 / URL / 시작-끝\n"
        "!변경 이름 / URL / 시작-끝\n"
        "!미리듣기 이름\n"
        "!타순 팀명 번호 / 닉네임\n"
        "!교체 팀명 번호 / 닉네임\n"
        "!이벤트저장 키 파일명\n"
        "!등장곡역할주기 @유저\n"
        "!등장곡역할회수 @유저\n"
        "!라인업 [팀명]\n"
        "※ 음성 채널 비어있으면 자동 퇴장"
    )

bot.run(TOKEN)
