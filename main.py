import discord
from discord.ext import commands
import yt_dlp
import os
import json
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
FIREBASE_KEY = os.getenv("FIREBASE_SERVICE_ACCOUNT")

if not firebase_admin._apps:
    cred_dict = json.loads(FIREBASE_KEY)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

current_team = {}
lineup_message = {}

ENTRANCE_ROLE_NAME = "등장곡 재생자"

def get_team(guild_id):
    return current_team.get(guild_id, "A팀")

def team_ref(team, path):
    return db.collection("teams").document(team).collection(path)

def is_admin(ctx):
    return ctx.author.guild_permissions.administrator

def has_entrance_role(member):
    return any(r.name == ENTRANCE_ROLE_NAME for r in member.roles)

async def get_or_create_role(guild):
    role = discord.utils.get(guild.roles, name=ENTRANCE_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ENTRANCE_ROLE_NAME)
    return role

async def connect_voice_by_guild(guild):
    vc = guild.voice_client
    if vc:
        return vc
    for m in guild.members:
        if m.voice:
            return await m.voice.channel.connect()
    return None

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

def game_state(team):
    ref = db.collection("teams").document(team).collection("state").document("game")
    if not ref.get().exists:
        ref.set({"currentOrder": 1})
    return ref

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "socket_timeout": 10,
    "source_address": "0.0.0.0"
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

async def play_youtube(guild, team, url, start, duration, order=None):
    vc = await connect_voice_by_guild(guild)
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

    if order is not None:
        game_state(team).update({"currentOrder": order})

async def play_song(guild, team, name, order):
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        return
    d = doc.to_dict()
    await play_youtube(guild, team, d["url"], d["start"], d["end"] - d["start"], order)

async def preview_song(ctx, name):
    team = get_team(ctx.guild.id)
    doc = team_ref(team, "entranceSongs").document(name).get()
    if not doc.exists:
        return
    d = doc.to_dict()
    await play_youtube(ctx.guild, team, d["url"], d["start"], 5)

async def play_event(guild, team, key):
    doc = team_ref(team, "events").document(key).get()
    if not doc.exists:
        return
    path = os.path.join("sounds", doc.to_dict()["file"])
    vc = await connect_voice_by_guild(guild)
    if vc and os.path.exists(path):
        if vc.is_playing():
            vc.stop()
        vc.play(discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(path),
            volume=get_volume(team)
        ))

@bot.event
async def on_ready():
    print("🔥 Railway 등장곡 봇 실행 완료")

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
                    await play_song(member.guild, team, nickname, int(d.id))
                    return

@bot.command(name="입장")
async def join(ctx):
    if ctx.author.voice:
        await connect_voice_by_guild(ctx.guild)

@bot.command(name="퇴장")
async def leave(ctx):
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect()

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
    team_ref(team, "entranceSongs").document(n).set({"url": u, "start": s, "end": e})
    await refresh_lineup(ctx)

@bot.command(name="변경")
async def change(ctx, *, args):
    if not is_admin(ctx):
        return
    team = get_team(ctx.guild.id)
    n, u, s, e = parse_song(args)
    team_ref(team, "entranceSongs").document(n).set(
        {"url": u, "start": s, "end": e}, merge=True
    )
    await refresh_lineup(ctx)

@bot.command(name="미리듣기")
async def preview(ctx, name: str):
    await preview_song(ctx, name)

@bot.command(name="타순")
async def set_order(ctx, num: int, *, args):
    if not is_admin(ctx):
        return
    team = get_team(ctx.guild.id)
    _, name = args.split("/", 1)
    team_ref(team, "lineup").document(str(num)).set({"name": name.strip()})
    await refresh_lineup(ctx)

@bot.command(name="타순삭제")
async def del_order(ctx, num: int):
    if not is_admin(ctx):
        return
    team_ref(get_team(ctx.guild.id), "lineup").document(str(num)).delete()
    await refresh_lineup(ctx)

@bot.command(name="교체")
async def change_player(ctx, num: int, *, args):
    if not is_admin(ctx):
        return
    team = get_team(ctx.guild.id)
    _, new = args.split("/", 1)
    team_ref(team, "lineup").document(str(num)).set({"name": new.strip()})
    await refresh_lineup(ctx)

@bot.command(name="이벤트저장")
async def save_event(ctx, key: str, filename: str):
    if not is_admin(ctx):
        return
    team_ref(get_team(ctx.guild.id), "events").document(key).set({"file": filename})

@bot.command(name="등장곡역할주기")
async def give_role(ctx, member: discord.Member):
    if not is_admin(ctx):
        return
    role = await get_or_create_role(ctx.guild)
    await member.add_roles(role)

@bot.command(name="등장곡역할회수")
async def remove_role(ctx, member: discord.Member):
    if not is_admin(ctx):
        return
    role = await get_or_create_role(ctx.guild)
    await member.remove_roles(role)

class Control(discord.ui.Button):
    def __init__(self, label, action):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.action = action

    async def callback(self, interaction):
        await interaction.response.defer()
        team = get_team(interaction.guild.id)
        if not has_entrance_role(interaction.user):
            return
        if self.action.startswith("num"):
            order = int(self.action.replace("num", ""))
            doc = team_ref(team, "lineup").document(str(order)).get()
            if doc.exists:
                await play_song(interaction.guild, team, doc.to_dict()["name"], order)
                await refresh_lineup(interaction)
        elif self.action == "stop":
            if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
                interaction.guild.voice_client.stop()
        elif self.action in ["strikeout", "fly", "homerun", "inning_change", "game_end"]:
            await play_event(interaction.guild, team, self.action)
        elif self.action == "next":
            st = game_state(team)
            cur = st.get().to_dict()["currentOrder"]
            nxt = cur + 1 if cur < 9 else 1
            doc = team_ref(team, "lineup").document(str(nxt)).get()
            if doc.exists:
                await play_song(interaction.guild, team, doc.to_dict()["name"], nxt)
                await refresh_lineup(interaction)

class LineupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, 10):
            self.add_item(Control(str(i), f"num{i}"))
        self.add_item(Control("▶️ 다음 타자", "next"))
        self.add_item(Control("❌ 삼진", "strikeout"))
        self.add_item(Control("🕊️ 플라이", "fly"))
        self.add_item(Control("💥 홈런", "homerun"))
        self.add_item(Control("🔁 이닝 교대", "inning_change"))
        self.add_item(Control("⏹ 중지", "stop"))
        self.add_item(Control("🔴 경기 종료", "game_end"))

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
        "!입장\n!퇴장\n!팀 팀명\n!저장 이름 / URL / 시작-끝\n!변경 이름 / URL / 시작-끝\n"
        "!미리듣기 이름\n!타순 번호 / 닉네임\n!교체 번호 / 닉네임\n!타순삭제 번호\n"
        "!라인업\n!볼륨 0~100\n!이벤트저장 키 파일명\n"
        "!등장곡역할주기 @유저\n!등장곡역할회수 @유저"
    )

bot.run(TOKEN)
