"""연결 재사용, 음성 상태 직렬화, stale client 정리, 제한된 재시도."""
from __future__ import annotations
import asyncio
import logging
import math
import time
import discord

log = logging.getLogger(__name__)


class VoiceError(RuntimeError):
    pass


class VoiceManager:
    def __init__(self):
        self.locks: dict[int, asyncio.Lock] = {}
        self.errors: dict[int, str] = {}
        self.stage_requests: dict[tuple[int, int], float] = {}

    def lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def check_channel(guild, channel):
        if channel is None:
            raise VoiceError('먼저 음성 채널에 들어가거나 웹에서 통화방을 선택하세요.')
        if channel.guild.id != guild.id:
            raise VoiceError('다른 서버의 음성 채널은 사용할 수 없습니다.')
        if channel.type not in {discord.ChannelType.voice, discord.ChannelType.stage_voice}:
            raise VoiceError('일반 음성 채널 또는 스테이지 채널을 선택하세요.')
        if guild.me is None:
            raise VoiceError('봇이 아직 서버 정보를 받지 못했습니다. 잠시 후 다시 시도하세요.')
        perms = channel.permissions_for(guild.me)
        required = [('view_channel', '채널 보기'), ('connect', '연결')]
        if channel.type == discord.ChannelType.voice:
            required.append(('speak', '말하기'))
        missing = [label for attr, label in required if not getattr(perms, attr, False)]
        if missing:
            raise VoiceError('봇의 채널 권한이 부족합니다: ' + ', '.join(missing))
        already = guild.voice_client and guild.voice_client.channel and guild.voice_client.channel.id == channel.id
        if getattr(channel, 'user_limit', 0) and len(channel.members) >= channel.user_limit and not already and not perms.move_members:
            raise VoiceError('통화방 인원이 가득 찼습니다. 인원 제한을 늘리거나 다른 방을 선택하세요.')

    @staticmethod
    def explain(exc: Exception) -> str:
        code = getattr(exc, 'code', None)
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return '음성 연결 시간이 초과됐습니다. 호스팅의 UDP 송수신/방화벽, 중복 실행 중인 봇을 확인하세요.'
        if code == 4017:
            return '음성 암호화(DAVE)가 필요합니다. requirements.txt로 재설치하고 davey 설치 여부를 확인하세요. (4017)'
        if code == 4014:
            return '서버에서 음성 연결이 해제됐습니다. 채널 권한·추방 여부·봇 중복 실행을 확인하세요. (4014)'
        if isinstance(exc, discord.Forbidden):
            return 'Discord에서 요청을 거부했습니다. 봇의 채널 보기·연결·말하기 권한을 확인하세요.'
        if isinstance(exc, VoiceError):
            return str(exc)
        if 'PyNaCl' in str(exc) or 'davey' in str(exc):
            return '음성 라이브러리가 누락됐습니다. python -m pip install -U -r requirements.txt 로 다시 설치하세요.'
        return f'음성 연결 실패 ({type(exc).__name__}' + (f', 코드 {code}' if code else '') + '). 서버 로그를 확인하세요.'

    async def _cleanup(self, guild):
        vc = guild.voice_client
        if vc is not None:
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=8)
            except Exception:
                log.warning('불완전한 음성 연결 정리: guild=%s', guild.id, exc_info=True)
            finally:
                if guild.voice_client is vc:
                    vc.cleanup()
        try:
            await asyncio.wait_for(guild.change_voice_state(channel=None), timeout=5)
        except Exception:
            log.debug('음성 상태 초기화 실패', exc_info=True)

    async def ensure_stage_speaker(self, guild, channel):
        """Do not send silent audio as an audience member. Never bypass Discord permissions."""
        if channel.type != discord.ChannelType.stage_voice:
            return
        for _ in range(10):
            state = getattr(guild.me, 'voice', None)
            if state and state.channel and state.channel.id == channel.id:
                break
            await asyncio.sleep(.2)
        else:
            raise VoiceError('스테이지 음성 상태를 아직 받지 못했습니다. 잠시 후 다시 재생하세요.')
        if not state.suppress:
            self.stage_requests.pop((guild.id, channel.id), None)
            return
        perms = channel.permissions_for(guild.me)
        if getattr(perms, 'mute_members', False):
            try:
                await asyncio.wait_for(guild.me.edit(suppress=False), timeout=8)
            except discord.Forbidden:
                raise VoiceError('스테이지 발언자 전환이 거부됐습니다. 이 채널의 멤버 음소거(Mute Members) 권한을 확인하거나 관리자가 봇을 발언자로 전환하세요.') from None
            # REST completion and Gateway updates are separate; verify before FFmpeg starts.
            for _ in range(15):
                current = getattr(guild.me, 'voice', None)
                if current and current.channel and current.channel.id == channel.id and not current.suppress:
                    return
                await asyncio.sleep(.2)
            raise VoiceError('발언자 전환을 요청했지만 아직 확인되지 않았습니다. Discord에서 발언자 상태를 확인한 후 다시 재생하세요.')
        key = (guild.id, channel.id)
        if getattr(perms, 'request_to_speak', False) and time.monotonic() - self.stage_requests.get(key, 0) > 30:
            try:
                await asyncio.wait_for(guild.me.request_to_speak(), timeout=8)
                self.stage_requests[key] = time.monotonic()
            except discord.Forbidden:
                pass
        raise VoiceError('스테이지에 연결됐지만 청중 상태입니다. 관리자가 봇을 발언자로 전환한 뒤 다시 재생하세요. 자동 전환하려면 봇에 이 채널의 멤버 음소거(Mute Members) 권한을 허용하세요.')

    async def connect(self, guild, channel=None):
        async with self.lock(guild.id):
            channel = channel or (guild.voice_client.channel if guild.voice_client else None)
            try:
                self.check_channel(guild, channel)
                vc = guild.voice_client
                if vc and vc.is_connected():
                    if vc.channel.id != channel.id:
                        try:
                            await asyncio.wait_for(vc.move_to(channel, timeout=20), timeout=25)
                        except Exception:
                            await self._cleanup(guild)
                            vc = None
                if not vc or not vc.is_connected():
                    last = None
                    for attempt in range(2):
                        if guild.voice_client:
                            await self._cleanup(guild)
                        try:
                            vc = await asyncio.wait_for(
                                channel.connect(timeout=25, reconnect=True, self_deaf=True), timeout=32)
                            if not vc.is_connected():
                                raise VoiceError('음성 연결이 완료되지 않았습니다.')
                            break
                        except asyncio.CancelledError:
                            await self._cleanup(guild)
                            raise
                        except Exception as exc:
                            last = exc
                            vc = None
                            log.warning('음성 연결 시도 %s/2 실패: guild=%s (%s)', attempt + 1, guild.id, type(exc).__name__, exc_info=True)
                            await self._cleanup(guild)
                            if isinstance(exc, discord.Forbidden) or getattr(exc, 'code', None) in {4017, 4014}:
                                break
                            if attempt == 0:
                                await asyncio.sleep(1)
                    if not vc:
                        raise VoiceError(self.explain(last)) from last
                # Speaker errors must keep the connection so a stage moderator can promote us.
                await self.ensure_stage_speaker(guild, channel)
                self.errors.pop(guild.id, None)
                return vc
            except Exception as exc:
                message = self.explain(exc)
                self.errors[guild.id] = message
                raise VoiceError(message) from exc

    async def disconnect(self, guild):
        async with self.lock(guild.id):
            await self._cleanup(guild)
            self.errors.pop(guild.id, None)

    def status(self, guild) -> dict:
        vc = guild.voice_client
        state = guild.me.voice if guild.me else None
        latency = getattr(vc, 'latency', None) if vc else None
        return {
            'connected': bool(vc and vc.is_connected()),
            'channelId': str(vc.channel.id) if vc and vc.channel else None,
            'channelName': vc.channel.name if vc and vc.channel else None,
            'playing': bool(vc and vc.is_playing()),
            'paused': bool(vc and vc.is_paused()),
            'serverMuted': bool(state and state.mute),
            'stage': bool(vc and vc.channel and vc.channel.type == discord.ChannelType.stage_voice),
            'stageAudience': bool(state and getattr(state, 'suppress', False) and vc and vc.channel and vc.channel.type == discord.ChannelType.stage_voice),
            'latencyMs': round(latency * 1000) if isinstance(latency, (int, float)) and math.isfinite(latency) else None,
            'error': self.errors.get(guild.id),
        }
