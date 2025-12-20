import discord
from discord.ext import commands
import yt_dlp
import os
import json
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

# ======================================================
# ENV
# ======================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
FIREBASE_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 없음")
if not FIREBASE_JSON:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT 없음")

# ======================================================
# Firebase
# ======================================================
cred = credentials.Certificate(json.loads(FIREBASE_JSON))
firebase_admin.initialize_app(cred)
db = firestore.client()

# ======================================================
# Discord
# ======================================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ======================================================
# 서버 상태
# ======================================================
current_team = {}        # guild_id -> team
lineup_message = {}      # guild_id -> message

def get_team(guild_id):
    return current_team.get(guild_id, "A팀")

def team_ref(guild_id, path):
    return db.collection("teams").document(get_team(guild_id)).collection(path)

# ======================================================
# 유틸
# ======================================================
def is_admin(ctx):
    return ctx.author.guild_permissions.administrator

async def connect_voice(member):
    if member.voice is None:
        return None
    vc = member.guild.voice_client
    if vc is None:
        vc = await member.voice.channel.connect()
    return vc

# ======================================================
# 오디오 (Opus 방식 – Railway 확정)
# ======================================================
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "source_address": "0.0.0.0"
}

async def play_youtube(member, url, start, duration):
    vc = await connect_voice(member)
    if vc is None:
        return

    if vc.is_playing():
        vc.stop()

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info["url"]

        source = await discord.FFmpegOpusAudio.from_probe(
            stream_url,
            before_options=f"-ss {start}",
            options=f"-t {duration}"
        )
        vc.play(source)

    except Exception as e:
        print("🔥 재생 오류:", e)

async def play_song(member, name):
    doc = team_ref(member.guild.id, "entranceSongs").document(name).get()
    if not doc.exists:
        return
    d = doc.to_dict()
    await play_youtube(member, d["url"], d["start"], d["end"] - d["start"])

async def preview_song(member, name):
    doc = team_ref(member.guild.id, "entranceSongs").document(name).get()
    if not doc.exists:
        return
    d = doc.to_dict()
    await play_youtube(member, d["url"], d["start"], 5)

async def play_event(member, key):
    doc = team_ref(member.guild.id, "events").document(key).get()
    if not doc.exists:
        return
    path = os.path.join("sounds", doc.to_dict()["file"])
    vc = await connect_voice(member)
    if vc and os.path.exists(path):
        if vc.is_playing():
            vc.stop()
        vc.play(discord.FFmpegPCMAudio(path))

async def stop_audio(member):
    vc = member.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()

# ======================================================
# 경기 상태
# ======================================================
def game_state(guild_id):
    ref = team_ref(guild_id, "state").document("game")
    if not ref.get().exists:
        ref.set({"currentOrder": 1})
    return ref

# ======================================================
# READY
# ======================================================
@bot.event
async def on_ready():
    print("🔥 등장곡 봇 실행 완료 (모든 기능 포함)")

# ======================================================
# 음성 명령어
# ======================================================
@bot.command(name="입장")
async def join(ctx):
    await connect_voice(ctx.author)

@bot.command(name="퇴장")
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()

# ======================================================
# 팀 선택
# ======================================================
@bot.command(name="팀")
async def set_team(ctx, team: str):
    current_team[ctx.guild.id] = team
    await ctx.send(f"✅ 현재 팀: {team}")

# ======================================================
# 등장곡 저장 / 변경 / 미리듣기
# ======================================================
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
    if not is_admin(ctx): return
    n,u,s,e = parse_song(args)
    team_ref(ctx.guild.id,"entranceSongs").document(n).set({
        "url":u,"start":s,"end":e
    })
    await refresh_lineup(ctx)

@bot.command(name="변경")
async def change(ctx, *, args):
    if not is_admin(ctx): return
    n,u,s,e = parse_song(args)
    team_ref(ctx.guild.id,"entranceSongs").document(n).set({
        "url":u,"start":s,"end":e
    }, merge=True)
    await refresh_lineup(ctx)

@bot.command(name="미리듣기")
async def preview(ctx, name:str):
    await preview_song(ctx.author, name)

# ======================================================
# 타순 관리
# ======================================================
@bot.command(name="타순")
async def set_order(ctx, num:int, *, args):
    if not is_admin(ctx): return
    _, name = args.split("/",1)
    team_ref(ctx.guild.id,"lineup").document(str(num)).set({"name":name.strip()})
    await refresh_lineup(ctx)

@bot.command(name="타순삭제")
async def del_order(ctx, num:int):
    if not is_admin(ctx): return
    team_ref(ctx.guild.id,"lineup").document(str(num)).delete()
    await refresh_lineup(ctx)

@bot.command(name="교체")
async def change_player(ctx, num:int, *, args):
    if not is_admin(ctx): return
    _,_, new = args.split("/",2)
    team_ref(ctx.guild.id,"lineup").document(str(num)).set({"name":new.strip()})
    await refresh_lineup(ctx)

# ======================================================
# 이벤트 저장
# ======================================================
@bot.command(name="이벤트저장")
async def save_event(ctx, key:str, filename:str):
    if not is_admin(ctx): return
    team_ref(ctx.guild.id,"events").document(key).set({"file":filename})

# ======================================================
# UI
# ======================================================
class Control(discord.ui.Button):
    def __init__(self,label,action):
        super().__init__(label=label,style=discord.ButtonStyle.primary)
        self.action=action

    async def callback(self,interaction):
        await interaction.response.defer()
        m=interaction.user
        gid=m.guild.id

        if self.action.startswith("num"):
            order=int(self.action.replace("num",""))
            doc=team_ref(gid,"lineup").document(str(order)).get()
            if doc.exists:
                await play_song(m,doc.to_dict()["name"])
            return

        if self.action=="stop":
            await stop_audio(m)
        elif self.action in ["strikeout","fly","homerun","inning_change","game_end"]:
            await play_event(m,self.action)
        elif self.action=="next":
            st=game_state(gid)
            cur=st.get().to_dict()["currentOrder"]
            nxt=cur+1 if cur<9 else 1
            st.update({"currentOrder":nxt})
            doc=team_ref(gid,"lineup").document(str(nxt)).get()
            if doc.exists:
                await play_song(m,doc.to_dict()["name"])

class LineupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1,10):
            self.add_item(Control(str(i),f"num{i}"))
        self.add_item(Control("▶ 다음 타자","next"))
        self.add_item(Control("❌ 삼진","strikeout"))
        self.add_item(Control("🕊 플라이","fly"))
        self.add_item(Control("💥 홈런","homerun"))
        self.add_item(Control("🔁 이닝 교대","inning_change"))
        self.add_item(Control("⏹ 중지","stop"))
        self.add_item(Control("🔴 경기 종료","game_end"))

# ======================================================
# 라인업 출력
# ======================================================
async def build_lineup(ctx):
    embed=discord.Embed(title=f"⚾ {get_team(ctx.guild.id)} 라인업")
    for i in range(1,10):
        d=team_ref(ctx.guild.id,"lineup").document(str(i)).get()
        embed.add_field(
            name=f"{i}번",
            value=d.to_dict()["name"] if d.exists else "-",
            inline=True
        )
    return embed

async def refresh_lineup(ctx):
    gid=ctx.guild.id
    if gid in lineup_message:
        await lineup_message[gid].edit(
            embed=await build_lineup(ctx),
            view=LineupView()
        )

@bot.command(name="라인업")
async def lineup(ctx):
    lineup_message[ctx.guild.id]=await ctx.send(
        embed=await build_lineup(ctx),
        view=LineupView()
    )

# ======================================================
# RUN
# ======================================================
bot.run(TOKEN)
