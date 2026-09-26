"""등장곡 봇 + 웹 관리 화면. 실행: python main.py"""
from __future__ import annotations
import asyncio
import logging
import shutil
import time
from dataclasses import replace
from pathlib import Path
import discord
from discord.ext import commands
from assets import Assets
from autoplay import select_auto_song
from config import Settings
from player import Player
from storage import Store
from validation import EVENTS, clean_name, parse_song_args
from voice import VoiceError
from webapp import WebPanel
from permissions import allowed, discord_role, DISCORD_ROLE_NAMES, LEGACY_PLAYER_ROLE

log = logging.getLogger('appearance')
ENTRANCE_ROLE = '등장곡 재생인'


def can_manage(member, settings: Settings) -> bool:
    return allowed(discord_role(member, settings.owner_id), 'play')


def can_register(member, settings: Settings) -> bool:
    return allowed(discord_role(member, settings.owner_id), 'music')


def text_channel(guild):
    candidates = [guild.system_channel] + list(guild.text_channels)
    for ch in candidates:
        if ch is not None and guild.me is not None:
            p = ch.permissions_for(guild.me)
            if p.view_channel and p.send_messages:
                return ch
    return None


def member_channel(member):
    return member.voice.channel if getattr(member, 'voice', None) else None


async def notify(channel, text=None, *, embed=None):
    if channel is not None:
        try:
            await channel.send(text, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.warning('안내 메시지를 보낼 수 없음 (텍스트 채널 권한 확인)')


class AppearanceBot(commands.Bot):
    def __init__(self, settings: Settings, *, slot: str = 'primary', label: str | None = None,
                 command_prefix: str = '!', web_owner: bool = True):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        # 통화방 입장 이벤트에는 members privileged intent가 필수가 아니다.
        intents.members = settings.enable_members_intent
        super().__init__(command_prefix=command_prefix, intents=intents, help_command=None,
                         allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.slot = 'secondary' if slot == 'secondary' else 'primary'
        self.label = label or (settings.secondary_bot_label if self.slot == 'secondary' else settings.primary_bot_label)
        self.web_owner = bool(web_owner)
        self.peer_bot: AppearanceBot | None = None
        self.store = Store(settings)
        self.assets = Assets(settings)
        self.player = Player(settings, self.store, self.assets)
        self.panel = WebPanel(self) if self.web_owner else None
        self.idle_tasks: dict[int, asyncio.Task] = {}
        self.join_cooldowns: dict[tuple[int, int], float] = {}
        self.lineup_messages: dict[int, discord.Message] = {}
        self.now_messages: dict[int, discord.Message] = {}

    async def setup_hook(self):
        await self.store.playback_settings()
        self.add_view(LineupView(self))
        if self.web_owner and self.settings.web_enabled and self.panel is not None:
            if self.settings.web_password:
                await self.panel.start()
            else:
                log.warning('WEB_ADMIN_PASSWORD 미설정: 봇만 실행합니다. 웹을 켜려면 12자 이상 비밀번호를 설정하세요.')

    async def close(self):
        for task in self.idle_tasks.values():
            task.cancel()
        await asyncio.gather(*self.idle_tasks.values(), return_exceptions=True)
        if self.panel is not None:
            await self.panel.close()
        await super().close()
        await self.player.close()
        await self.store.close()

    async def on_ready(self):
        log.info('Discord 로그인 완료 [%s]: %s / discord.py %s', self.label, self.user, discord.__version__)
        if not shutil.which(self.settings.ffmpeg):
            log.error('FFmpeg 미설치: 통화방 접속은 가능해도 오디오 재생이 불가능합니다.')
        for guild in self.guilds:
            if self.slot == 'primary' and self.settings.secondary_token:
                await self.store.ensure_dual_team_defaults(guild.id)
            await self.manage_idle(guild)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        original = getattr(error, 'original', error)
        if isinstance(error, commands.MissingRequiredArgument):
            msg = f'입력값이 부족합니다. `{"!2" if self.slot == "secondary" else "!"}도움`을 확인하세요. ({error.param.name})'
        elif isinstance(error, commands.NoPrivateMessage):
            msg = '서버의 텍스트 채널에서 사용하세요.'
        elif isinstance(error, commands.CheckFailure):
            msg = '이 명령의 권한이 없습니다. 재생은 등장곡 재생자 이상, 등록·수정·삭제는 등장곡 등록/삭제자 이상, 운영 설정은 관리자만 가능합니다.'
        elif isinstance(error, commands.BadArgument):
            msg = f'입력 형식이 잘못됐습니다. `{"!2" if self.slot == "secondary" else "!"}도움`을 확인하세요.'
        elif isinstance(original, (ValueError, VoiceError)):
            msg = str(original)
        elif isinstance(original, discord.Forbidden):
            msg = '봇 권한이 부족합니다. 텍스트·통화방 권한과 역할 순서를 확인하세요.'
        else:
            log.error('명령어 오류: %s', type(original).__name__, exc_info=(type(original), original, original.__traceback__))
            msg = '처리 중 오류가 발생했습니다. 웹 진단 탭 또는 서버 로그를 확인하세요.'
        await notify(ctx.channel, '❌ ' + msg)

    async def notification_channel(self, guild, fallback=None):
        """저장된 봇 출력 채널을 반환하고, 사용할 수 없으면 안전한 기본 채널을 사용합니다."""
        channel_id = await self.store.get_notification_channel(guild.id, self.slot)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel is not None and guild.me is not None:
                try:
                    perms = channel.permissions_for(guild.me)
                    if perms.view_channel and perms.send_messages and perms.embed_links:
                        return channel
                except AttributeError:
                    pass
            log.warning('지정된 알림 채널(%s)을 사용할 수 없어 자동 채널을 사용합니다.', channel_id)
        return fallback if fallback is not None else text_channel(guild)

    def peer_voice_channel(self, guild):
        peer = self.peer_bot
        if peer is None:
            return None
        peer_guild = peer.get_guild(guild.id)
        if peer_guild is None:
            return None
        vc = peer_guild.voice_client
        return vc.channel if vc and vc.channel else None

    def ensure_distinct_voice_channel(self, guild, channel):
        """청팀/백팀 봇이 같은 서버의 같은 통화방에 동시에 들어가는 것을 차단한다."""
        if channel is None:
            return
        peer_channel = self.peer_voice_channel(guild)
        if peer_channel is not None and int(peer_channel.id) == int(channel.id):
            peer_label = getattr(self.peer_bot, 'label', '다른 봇')
            raise VoiceError(
                f'{self.label}은 {peer_label}이 들어가 있는 “{peer_channel.name}” 통화방에는 함께 들어갈 수 없습니다. '
                '청팀과 백팀은 서로 다른 통화방을 선택하세요.'
            )

    async def now_embed(self, guild, channel, team: str):
        playing = self.player.now.get(guild.id, {})
        state = await self.store.state(team)
        embed = discord.Embed(title=f'🎶 {self.label} · 등장곡 플레이어', color=discord.Color.green())
        embed.add_field(name='현재 곡', value=playing.get('title', '대기 중'), inline=False)
        embed.add_field(name='팀', value=team)
        embed.add_field(name='볼륨', value=f'{round(state["volume"] * 100)}%')
        embed.add_field(name='타순', value=str(state['currentOrder']))
        channel = await self.notification_channel(guild, channel)
        if channel is None:
            return
        old = self.now_messages.get(guild.id)
        try:
            if old and old.channel.id == channel.id:
                await old.edit(embed=embed)
            else:
                self.now_messages[guild.id] = await channel.send(embed=embed)
        except discord.NotFound:
            self.now_messages[guild.id] = await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning('재생 안내 메시지 갱신 실패')

    async def lineup_embed(self, guild):
        team = await self.store.get_team(guild.id, self.slot)
        state = await self.store.state(team)
        lineup = await self.store.lineup(team)
        embed = discord.Embed(title=f'⚾ {self.label} · {team} 라인업 (현재 {state["currentOrder"]}번)', color=discord.Color.blurple())
        for i in range(1, 10):
            embed.add_field(name=f'{i}번', value=lineup.get(str(i)) or '-', inline=True)
        embed.set_footer(text='재생자 이상 조작 가능 · 등록/삭제자 음악 편집 · 관리자 타순 설정')
        return embed

    async def refresh_lineup(self, guild):
        message = self.lineup_messages.get(guild.id)
        if message:
            try:
                await message.edit(embed=await self.lineup_embed(guild), view=LineupView(self))
            except discord.HTTPException:
                self.lineup_messages.pop(guild.id, None)

    async def play_order(self, guild, order: int, channel, voice_channel=None):
        if not 1 <= order <= 9:
            raise ValueError('타순은 1~9번입니다.')
        team = await self.store.get_team(guild.id, self.slot)
        name = (await self.store.lineup(team)).get(str(order))
        song = await self.store.song(team, name) if name else None
        if song is None:
            raise ValueError(f'{order}번 타자의 이름과 등장곡을 먼저 등록하세요.')
        self.ensure_distinct_voice_channel(guild, voice_channel)
        ok = await self.player.play(guild, team, song, voice_channel, order=order)
        if ok:
            await self.now_embed(guild, channel, team)
            await self.refresh_lineup(guild)
        await self.manage_idle(guild)

    async def manage_idle(self, guild):
        vc = guild.voice_client
        empty = bool(vc and vc.channel and not any(not m.bot for m in vc.channel.members))
        task = self.idle_tasks.get(guild.id)
        if empty:
            if not task or task.done():
                self.idle_tasks[guild.id] = asyncio.create_task(self._idle(guild))
        elif task and task is not asyncio.current_task():
            task.cancel()
            self.idle_tasks.pop(guild.id, None)

    async def _idle(self, guild):
        try:
            wait = self.settings.idle_seconds
            await asyncio.sleep(max(0, wait - 30))
            vc = guild.voice_client
            if not vc or not vc.channel or any(not m.bot for m in vc.channel.members):
                return
            await notify(await self.notification_channel(guild), f'⏱ [{self.label}] 통화방에 사람이 없어 {min(30, wait)}초 후 퇴장합니다.')
            await asyncio.sleep(min(30, wait))
            vc = guild.voice_client
            if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
                await self.player.leave(guild)
                await notify(await self.notification_channel(guild), f'🔇 [{self.label}] 빈 통화방에서 자동 퇴장했습니다.')
        finally:
            if self.idle_tasks.get(guild.id) is asyncio.current_task():
                self.idle_tasks.pop(guild.id, None)

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        await self.manage_idle(guild)

        # 관리자가 Discord에서 봇을 직접 이동시킨 경우에도 두 봇이 같은 통화방에 겹치지 않게 한다.
        if member.bot:
            if self.user and member.id == self.user.id and after.channel is not None and before.channel != after.channel:
                try:
                    self.ensure_distinct_voice_channel(guild, after.channel)
                except VoiceError as exc:
                    await self.player.leave(guild)
                    await notify(await self.notification_channel(guild), '❌ ' + str(exc))
            return

        # 퇴장/음소거 같은 상태 변경은 무시하고, 새 입장 또는 다른 통화방으로 이동한 경우만 처리한다.
        if after.channel is None or before.channel == after.channel:
            return
        auto_setting = await self.store.playback_settings()
        if not auto_setting['enabled']:
            return
        key = (guild.id, member.id)
        now = time.monotonic()
        # Discord가 짧은 시간 안에 중복 상태 이벤트를 보내는 경우만 막는다.
        # 실제 퇴장 후 재입장은 최대한 바로 다시 재생될 수 있게 기존 5초보다 짧게 둔다.
        if now - self.join_cooldowns.get(key, 0) < 1.0:
            return
        self.join_cooldowns[key] = now
        if len(self.join_cooldowns) > 10000:
            self.join_cooldowns = {k: t for k, t in self.join_cooldowns.items() if now - t < 60}
        channel = await self.notification_channel(guild)
        try:
            # 2봇 모드에서는 각 봇에 지정한 팀만 자동 재생한다.
            # 그래야 청팀 봇/백팀 봇이 같은 입장 이벤트를 동시에 잡아채지 않는다.
            # 보조 봇 토큰이 없는 1봇 모드에서는 기존 호환성을 위해 다른 팀도 이어서 찾는다.
            current = await self.store.get_team(guild.id, self.slot)
            teams = [current] if self.settings.secondary_token else [current] + [t for t in await self.store.teams() if t != current]
            member_id = str(member.id)
            has_legacy_role = can_manage(member, self.settings)

            for team in teams:
                songs = await self.store.songs(team)
                lineup = await self.store.lineup(team)

                # ID가 있으면 역할/타순 없이 바로 연결하고, ID 없는 기존 곡만 예전 규칙을 유지한다.
                song, order = select_auto_song(
                    songs, lineup, member_id, member.display_name, has_legacy_role
                )
                if not song:
                    continue
                # after.channel이 이번 이벤트의 확정된 목적 통화방이다. member.voice 캐시를 다시 검사하지 않는다.
                if guild.voice_client and guild.voice_client.channel and guild.voice_client.channel.id != after.channel.id:
                    # 다른 방에서 사람이 사용 중이면 자동 등장곡 때문에 봇을 강제로 이동시키지 않는다.
                    if any(not m.bot for m in guild.voice_client.channel.members):
                        return
                self.ensure_distinct_voice_channel(guild, after.channel)
                ok = await self.player.play(guild, team, song, after.channel, order, auto_revision=auto_setting['revision'])
                if ok:
                    await self.store.set_team(guild.id, team, self.slot)
                    await self.now_embed(guild, channel, team)
                return
        except (ValueError, VoiceError) as exc:
            await notify(channel, f'❌ [{self.label}] 자동 등장곡: ' + str(exc))
        except Exception:
            log.exception('자동 등장곡 오류')
            await notify(channel, f'❌ [{self.label}] 자동 등장곡 처리 오류. 웹 진단 탭과 서버 로그를 확인하세요.')


class Control(discord.ui.Button):
    def __init__(self, app: AppearanceBot, label: str, action: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary,
                         custom_id=f'appearance:v2:{action}')
        self.app, self.action = app, action

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        team = await self.app.store.get_team(guild.id, self.app.slot)
        voice = member_channel(interaction.user)
        if voice is not None:
            self.app.ensure_distinct_voice_channel(guild, voice)
        if self.action == 'stop':
            self.app.player.stop(guild)
        elif self.action == 'next':
            order = ((await self.app.store.state(team))['currentOrder'] % 9) + 1
            await self.app.play_order(guild, order, interaction.channel, voice)
            return
        elif self.action.startswith('num'):
            await self.app.play_order(guild, int(self.action[3:]), interaction.channel, voice)
            return
        else:
            await self.app.player.event(guild, team, self.action, voice)
        await self.app.now_embed(guild, interaction.channel, team)
        await self.app.manage_idle(guild)


class LineupView(discord.ui.View):
    def __init__(self, app: AppearanceBot):
        super().__init__(timeout=None)
        self.app = app
        for i in range(1, 10):
            self.add_item(Control(app, str(i), f'num{i}'))
        self.add_item(Control(app, '다음 타자', 'next'))
        for key in ['lineup', 'homerun', 'strikeout', '4ball', 'fullcount', 'look', 'fly', 'out', 'steal', 'inning_change', 'game_end1']:
            self.add_item(Control(app, EVENTS[key], key))
        self.add_item(Control(app, '⏹ 정지', 'stop'))

    async def interaction_check(self, interaction):
        if not interaction.guild or not can_manage(interaction.user, self.app.settings):
            await interaction.response.send_message('등장곡 재생자 이상 권한이 필요합니다. 기존 등장곡 재생인 역할도 지원합니다.', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        log.error('라인업 버튼 오류 (%s)', type(error).__name__, exc_info=(type(error), error, error.__traceback__))
        message = str(error) if isinstance(error, (ValueError, VoiceError)) else '처리 중 오류가 발생했습니다. 서버 로그를 확인하세요.'
        if interaction.response.is_done():
            await interaction.followup.send('❌ ' + message, ephemeral=True)
        else:
            await interaction.response.send_message('❌ ' + message, ephemeral=True)


def install_commands(bot: AppearanceBot):
    def manager():
        return commands.check(lambda ctx: can_manage(ctx.author, bot.settings))

    def registrar():
        return commands.check(lambda ctx: can_register(ctx.author, bot.settings))

    def admin_only():
        return commands.check(lambda ctx: bool(
            ctx.author and (
                ctx.author.id == bot.settings.owner_id
                or getattr(ctx.author, 'guild_permissions', discord.Permissions.none()).administrator
            )
        ))

    @bot.check
    async def server_only(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    @bot.command(name='알림채널설정', aliases=['출력채널설정', '로그채널설정'])
    @admin_only()
    async def set_notification_channel(ctx, channel: discord.TextChannel = None):
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            raise ValueError('일반 텍스트 채널에서 실행하거나 `!알림채널설정 #채널`로 지정하세요.')
        if ctx.guild.me is None:
            raise ValueError('봇의 서버 멤버 정보를 확인할 수 없습니다.')
        perms = target.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ValueError('지정할 채널에서 봇에게 채널 보기, 메시지 보내기, 링크 임베드 권한이 필요합니다.')
        await bot.store.set_notification_channel(ctx.guild.id, target.id, bot.slot)
        old = bot.now_messages.pop(ctx.guild.id, None)
        if old and old.channel.id != target.id:
            try:
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await ctx.send(f'✅ {bot.label} 알림 채널을 {target.mention} 로 지정했습니다. 재시작/재배포 후에도 유지됩니다.')

    @bot.command(name='알림채널')
    async def show_notification_channel(ctx):
        channel_id = await bot.store.get_notification_channel(ctx.guild.id, bot.slot)
        if channel_id:
            channel = ctx.guild.get_channel(channel_id)
            if channel is not None:
                await ctx.send(f'📢 현재 {bot.label} 알림 채널: {channel.mention}')
                return
            await ctx.send(f'⚠️ 저장된 알림 채널(ID: {channel_id})을 찾을 수 없습니다. `!알림채널설정`으로 다시 지정하세요.')
            return
        await ctx.send('📢 별도 알림 채널이 없습니다. 현재는 봇이 메시지를 보낼 수 있는 텍스트 채널을 자동 선택합니다.')

    @bot.command(name='알림채널해제', aliases=['출력채널해제'])
    @admin_only()
    async def clear_notification_channel(ctx):
        await bot.store.set_notification_channel(ctx.guild.id, None, bot.slot)
        bot.now_messages.pop(ctx.guild.id, None)
        await ctx.send(f'✅ {bot.label} 알림 채널 지정을 해제했습니다. 이제 메시지를 보낼 수 있는 채널을 자동 선택합니다.')

    @bot.command(name='입장')
    @manager()
    async def join(ctx):
        target_channel = member_channel(ctx.author)
        bot.ensure_distinct_voice_channel(ctx.guild, target_channel)
        vc = await bot.player.voice.connect(ctx.guild, target_channel)
        await ctx.send(f'🔊 {vc.channel.name}에 연결했습니다.')
        await bot.manage_idle(ctx.guild)

    @bot.command(name='퇴장')
    @manager()
    async def leave(ctx):
        await bot.player.leave(ctx.guild)
        await ctx.send('🔇 통화방에서 퇴장했습니다.')

    @bot.command(name='정지')
    @manager()
    async def stop(ctx):
        bot.player.stop(ctx.guild)
        await ctx.send('⏹ 재생을 정지했습니다.')

    @bot.command(name='팀')
    @admin_only()
    async def team(ctx, *, name: str):
        await bot.store.set_team(ctx.guild.id, clean_name(name), bot.slot)
        await ctx.send(f'✅ {bot.label} 현재 팀: ' + name)

    @bot.command(name='볼륨')
    @manager()
    async def volume(ctx, value: int):
        await bot.player.volume(ctx.guild, await bot.store.get_team(ctx.guild.id, bot.slot), value)
        await ctx.send(f'🔊 볼륨 {value}% (재생 중인 곡에도 반영)')

    @bot.command(name='저장', aliases=['변경'])
    @registrar()
    async def save(ctx, *, args: str):
        song = parse_song_args(args)
        await bot.store.save_song(await bot.store.get_team(ctx.guild.id, bot.slot), song)
        await ctx.send('✅ 등장곡 저장: ' + song['name'])

    @bot.command(name='미리듣기')
    @manager()
    async def preview(ctx, *, name: str):
        team = await bot.store.get_team(ctx.guild.id, bot.slot)
        song = await bot.store.song(team, clean_name(name))
        if not song:
            raise ValueError('등록된 등장곡이 없습니다.')
        target_channel = member_channel(ctx.author)
        bot.ensure_distinct_voice_channel(ctx.guild, target_channel)
        ok = await bot.player.play(ctx.guild, team, song, target_channel, preview=True)
        if ok:
            await bot.now_embed(ctx.guild, ctx.channel, team)
        await bot.manage_idle(ctx.guild)

    @bot.command(name='재생')
    @manager()
    async def play(ctx, *, name: str):
        team = await bot.store.get_team(ctx.guild.id, bot.slot)
        song = await bot.store.song(team, clean_name(name))
        if not song:
            raise ValueError('등록된 등장곡이 없습니다.')
        target_channel = member_channel(ctx.author)
        bot.ensure_distinct_voice_channel(ctx.guild, target_channel)
        ok = await bot.player.play(ctx.guild, team, song, target_channel)
        if ok:
            await bot.now_embed(ctx.guild, ctx.channel, team)
        await bot.manage_idle(ctx.guild)

    async def play_library_category(ctx, category: str, name: str):
        team = await bot.store.get_team(ctx.guild.id, bot.slot)
        song = await bot.store.song(team, clean_name(name), category)
        if not song:
            raise ValueError('이 종류에 등록된 곡이 없습니다. 웹 음악 라이브러리를 확인하세요.')
        target_channel = member_channel(ctx.author)
        bot.ensure_distinct_voice_channel(ctx.guild, target_channel)
        ok = await bot.player.play(ctx.guild, team, song, target_channel)
        if ok:
            await bot.now_embed(ctx.guild, ctx.channel, team)
        await bot.manage_idle(ctx.guild)

    @bot.command(name='응원가', aliases=['응원가재생'])
    @manager()
    async def play_cheer(ctx, *, name: str):
        await play_library_category(ctx, 'cheer', name)

    @bot.command(name='상황곡', aliases=['상황별노래'])
    @manager()
    async def play_situation(ctx, *, name: str):
        await play_library_category(ctx, 'situation', name)

    @bot.command(name='응원가저장')
    @registrar()
    async def save_cheer(ctx, *, args: str):
        song = {**parse_song_args(args), 'category': 'cheer'}
        await bot.store.save_song(await bot.store.get_team(ctx.guild.id, bot.slot), song)
        await ctx.send('✅ 응원가 저장: ' + song['name'])

    @bot.command(name='상황곡저장')
    @registrar()
    async def save_situation(ctx, *, args: str):
        song = {**parse_song_args(args), 'category': 'situation'}
        await bot.store.save_song(await bot.store.get_team(ctx.guild.id, bot.slot), song)
        await ctx.send('✅ 상황별 노래 저장: ' + song['name'])

    @bot.command(name='타순', aliases=['교체'])
    @admin_only()
    async def order(ctx, num: int, *, args: str):
        if not 1 <= num <= 9:
            raise ValueError('타순은 1~9번입니다.')
        name = args.lstrip('/').strip()
        team = await bot.store.get_team(ctx.guild.id, bot.slot)
        await bot.store.save_lineup(team, {str(num): clean_name(name)})
        await ctx.send(f'✅ {num}번 타자: {name}')
        await bot.refresh_lineup(ctx.guild)

    @bot.command(name='이벤트저장')
    @registrar()
    async def save_event(ctx, key: str, *, filename: str):
        if key not in EVENTS:
            raise ValueError('효과음 키: ' + ', '.join(EVENTS))
        bot.assets.legacy_file(key, filename)
        await bot.store.set_event(await bot.store.get_team(ctx.guild.id, bot.slot), key, {'file': filename})
        await ctx.send('✅ 효과음 저장: ' + key)

    async def role_change(ctx, member, remove: bool, role_key='player', exclusive=False):
        if discord_role(ctx.author, bot.settings.owner_id) != 'admin':
            raise commands.CheckFailure()
        if not ctx.guild.me.guild_permissions.manage_roles:
            raise ValueError('봇에 역할 관리 권한이 필요합니다.')
        target_name = DISCORD_ROLE_NAMES[role_key]
        role = discord.utils.get(ctx.guild.roles, name=target_name)
        managed_names = {*DISCORD_ROLE_NAMES.values(), LEGACY_PLAYER_ROLE}
        previous = [r for r in member.roles if r.name in managed_names and
                    (exclusive or r.name == target_name or (role_key == 'player' and r.name == LEGACY_PLAYER_ROLE))]
        if any(r >= ctx.guild.me.top_role for r in previous) or (role and role >= ctx.guild.me.top_role):
            raise ValueError('서버 역할 목록에서 봇 역할을 관리할 등장곡 역할보다 위로 올리세요.')
        if remove:
            if previous: await member.remove_roles(*previous, reason='등장곡 권한 회수')
        else:
            if role is None:
                role = await ctx.guild.create_role(name=target_name, reason='등장곡 권한 분리')
            # Add first; never strip other unrelated Discord roles.
            await member.add_roles(role, reason='등장곡 권한 설정')
            obsolete = [r for r in previous if r.id != role.id] if exclusive else []
            if obsolete: await member.remove_roles(*obsolete, reason='등장곡 권한 변경')
        await ctx.send(('권한 회수: ' if remove else '권한 설정: ') + member.display_name + ' · ' + target_name)

    @bot.command(name='등장곡역할주기')
    @admin_only()
    async def give_role(ctx, member: discord.Member):
        await role_change(ctx, member, False)

    @bot.command(name='등장곡역할회수')
    @admin_only()
    async def remove_role(ctx, member: discord.Member):
        await role_change(ctx, member, True)

    @bot.command(name='권한설정')
    @admin_only()
    async def set_role(ctx, member: discord.Member, *, role_name: str):
        names = {**{v: k for k, v in DISCORD_ROLE_NAMES.items()},
                 '등록삭제자': 'registrar', '등록/삭제자': 'registrar', '재생자': 'player',
                 'registrar': 'registrar', 'player': 'player', 'user': 'user'}
        key = names.get(role_name.strip())
        if key is None:
            raise ValueError('사용법: !권한설정 @사용자 등록삭제자 / 재생자 / 유저 (하나만 입력)')
        await role_change(ctx, member, False, key, exclusive=True)

    @bot.command(name='자동등장곡')
    @manager()
    async def auto_entrance(ctx, value: str = ''):
        value = value.strip().lower()
        if value:
            if value not in {'on', 'off', '켜기', '끄기'}:
                raise ValueError('사용법: !자동등장곡 on 또는 !자동등장곡 off')
            setting = await bot.store.set_auto_entrance(value in {'on', '켜기'})
        else:
            setting = await bot.store.playback_settings()
        await ctx.send('전체 자동 등장곡 ' + ('ON' if setting['enabled'] else 'OFF') +
                       ' · 모든 서버/팀/청팀·백팀 봇에 공통 적용. 수동 재생은 유지됩니다.')

    async def delete_library_song(ctx, name, category):
        team = await bot.store.get_team(ctx.guild.id, bot.slot)
        await bot.store.delete_song(team, clean_name(name), category)
        await ctx.send('곡 등록과 관련 연결을 삭제했습니다. 업로드 원본 파일은 백업을 위해 보관합니다.')

    @bot.command(name='삭제')
    @registrar()
    async def delete_entrance(ctx, *, name: str):
        await delete_library_song(ctx, name, 'entrance')

    @bot.command(name='응원가삭제')
    @registrar()
    async def delete_cheer(ctx, *, name: str):
        await delete_library_song(ctx, name, 'cheer')

    @bot.command(name='상황곡삭제')
    @registrar()
    async def delete_situation(ctx, *, name: str):
        await delete_library_song(ctx, name, 'situation')

    @bot.command(name='이름변경')
    @registrar()
    async def rename(ctx, *, args: str):
        team = await bot.store.get_team(ctx.guild.id, bot.slot)
        if ' / ' in args:
            if not can_manage(ctx.author, bot.settings):
                raise commands.CheckFailure()
            old, new = (a.strip() for a in args.split(' / ', 1))
        else:
            old, new = ctx.author.display_name, args.strip()
            song = await bot.store.song(team, old)
            if song and song.get('memberId') and song['memberId'] != str(ctx.author.id) and not can_manage(ctx.author, bot.settings):
                raise commands.CheckFailure()
        await bot.store.rename(team, old, new)
        await ctx.send(f'✅ {old} → {new} (등장곡과 타순 이름만 변경; Discord 닉네임은 그대로)')
        await bot.refresh_lineup(ctx.guild)

    @bot.command(name='라인업')
    async def lineup(ctx):
        bot.lineup_messages[ctx.guild.id] = await ctx.send(embed=await bot.lineup_embed(ctx.guild), view=LineupView(bot))

    @bot.command(name='웹')
    async def panel(ctx):
        website = bot.settings.web_url or bot.settings.public_url
        if website:
            await ctx.send('🌐 등장곡 관리: ' + website + '\n승인된 웹 계정 또는 admin 계정으로 로그인하세요.')
        else:
            await ctx.send('웹 서버가 실행 중인 PC에서 `http://localhost:8080`으로 접속하세요. PORT를 변경했다면 그 포트를 사용하세요. 외부 공개 주소는 PUBLIC_URL에 설정하세요.')

    @bot.command(name='진단')
    async def diagnosis(ctx):
        from webapp import version
        from persistence import storage_status
        files = storage_status(bot.settings)
        channel = member_channel(ctx.author)
        try:
            bot.player.voice.check_channel(ctx.guild, channel)
            permission = '선택한 통화방 권한 정상'
        except VoiceError as exc:
            permission = str(exc)
        st = bot.player.voice.status(ctx.guild)
        await ctx.send(
            f'🔎 discord.py {discord.__version__} / PyNaCl {version("PyNaCl")} / davey {version("davey")}\n'
            f'FFmpeg: {"설치됨" if shutil.which(bot.settings.ffmpeg) else "없음"} / 저장소: {bot.store.mode}\n'
            f'{permission}\n현재 연결: {st["channelName"] or "없음"} / 서버 음소거: {st["serverMuted"]}\n'
            f'최근 연결 오류: {st["error"] or "없음"}\n'
            f'오디오 저장: {files["label"]}\n{files["message"]}'
        )

    @bot.command(name='도움', aliases=['help'])
    async def help_command(ctx):
        p = '!2' if bot.slot == 'secondary' else '!'
        await ctx.send(
            f'**⚾ {bot.label} 사용법**\n'
            f'`{p}입장` · `{p}퇴장` · `{p}정지` · `{p}진단` · `{p}웹`\n'
            f'`{p}팀 팀명` · `{p}볼륨 0~100`\n'
            f'`{p}저장 이름 / YouTube주소 / 0:10~0:40` (`{p}변경`도 가능)\n'
            f'`{p}미리듣기 이름` (5초) · `{p}재생 이름` (등장곡)\n'
            f'`{p}응원가 이름` · `{p}상황곡 이름`\n'
            f'`{p}응원가저장 이름 / YouTube주소 / 0:00~1:00`\n'
            f'`{p}상황곡저장 이름 / YouTube주소 / 0:00~0:30`\n'
            f'`{p}타순 1 / 이름` · `{p}교체 1 / 이름` · `{p}라인업`\n'
            f'`{p}알림채널설정 #채널` · `{p}알림채널` · `{p}알림채널해제`\n'
            f'`{p}이벤트저장 키 파일명` (파일은 sounds 안에 있어야 함)\n'
            f'`{p}등장곡역할주기 @유저` · `{p}등장곡역할회수 @유저` (관리자/OWNER_ID 전용)\n'
            f'`{p}이름변경 새이름` · `{p}이름변경 기존이름 / 새이름`\n'
            f'`{p}자동등장곡 on/off` · `{p}삭제 이름` · `{p}응원가삭제 이름` · `{p}상황곡삭제 이름`\n'
            f'`{p}권한설정 @유저 등록삭제자/재생자/유저` (권한 하나만 입력, 관리자 전용)\n'
            '등록·삭제는 등록/삭제자 이상, 재생은 재생자 이상, 팀·타순·회원은 관리자만 가능합니다.\n'
            '2봇 모드에서는 각 봇에 웹에서 지정한 경기 팀의 등장곡만 자동 재생합니다.'
        )


def create_bot(settings: Settings, *, slot: str = 'primary', label: str | None = None,
               command_prefix: str = '!', web_owner: bool = True) -> AppearanceBot:
    bot = AppearanceBot(settings, slot=slot, label=label, command_prefix=command_prefix, web_owner=web_owner)
    install_commands(bot)
    return bot


async def run():
    settings = Settings.from_env()
    # Runtime DB is SQLite. Optional legacy Firestore recovery is a one-time, non-fatal import.
    if __import__('os').getenv('MIGRATE_FIRESTORE_ONCE', '').strip().lower() in {'1','true','yes','on'}:
        from legacy_migration import migrate_firestore_once
        await asyncio.to_thread(migrate_firestore_once, settings.data_dir)
    primary = create_bot(
        settings, slot='primary', label=settings.primary_bot_label,
        command_prefix='!', web_owner=True,
    )
    secondary = None
    if settings.secondary_token and not settings.web_only:
        secondary_settings = replace(settings, token=settings.secondary_token, web_enabled=False)
        secondary = create_bot(
            secondary_settings, slot='secondary', label=settings.secondary_bot_label,
            command_prefix='!2', web_owner=False,
        )
        primary.peer_bot = secondary
        secondary.peer_bot = primary
        log.info('2봇 모드 활성화: %s + %s', primary.label, secondary.label)
    elif not settings.web_only:
        log.info('1봇 모드: DISCORD_TOKEN_SECONDARY가 없어 기존 봇만 실행합니다.')

    try:
        if settings.web_only:
            log.warning('WEB_ONLY=true: 웹 관리 화면만 실행합니다. Discord 접속/재생은 하지 않습니다.')
            if primary.panel is None:
                raise RuntimeError('웹 패널을 시작할 수 없습니다.')
            await primary.panel.start()
            await asyncio.Event().wait()
        elif secondary is not None:
            await asyncio.gather(
                primary.start(settings.token),
                secondary.start(settings.secondary_token),
            )
        else:
            await primary.start(settings.token)
    except discord.PrivilegedIntentsRequired:
        log.error('Discord Developer Portal → Bot → Message Content Intent를 켜세요. ENABLE_MEMBERS_INTENT=true면 Server Members Intent도 켜야 합니다.')
        raise
    except discord.LoginFailure:
        log.error('Discord 봇 토큰이 유효하지 않습니다. DISCORD_TOKEN / DISCORD_TOKEN_SECONDARY를 확인하세요. 토큰을 채팅에 보내지 마세요.')
        raise
    finally:
        bots = [primary] + ([secondary] if secondary is not None else [])
        await asyncio.gather(*(b.close() for b in bots), return_exceptions=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
