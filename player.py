"""재생 준비는 별도 스레드에서 처리. 정지/퇴장 후 늦게 추출된 곡이 재생되지 않도록 세대 번호 사용."""
from __future__ import annotations
import asyncio
import base64
import logging
import os
import random
import shlex
from collections import defaultdict
from pathlib import Path
import discord
import yt_dlp
from assets import Assets
from config import Settings
from storage import Store
from validation import youtube_url, time_range
from voice import VoiceManager, VoiceError

log = logging.getLogger(__name__)


class Player:
    def __init__(self, settings: Settings, store: Store, assets: Assets):
        self.settings, self.store, self.assets = settings, store, assets
        self.voice = VoiceManager()
        self.generation = defaultdict(int)
        self.now: dict[int, dict] = {}
        self.extract_slots = asyncio.Semaphore(2)
        self.extract_tasks: set[asyncio.Task] = set()
        self.cookie_path = self._cookies()

    def _cookies(self) -> str | None:
        if self.settings.cookies_path:
            path = Path(self.settings.cookies_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError('YTDLP_COOKIES_PATH 파일이 없습니다.')
            return str(path)
        if self.settings.cookies_b64:
            try:
                data = base64.b64decode(self.settings.cookies_b64, validate=True)
            except ValueError:
                raise ValueError('YTDLP_COOKIES_B64가 올바른 Base64가 아닙니다.') from None
            path = self.settings.data_dir / 'ytdlp-cookies.txt'
            path.write_bytes(data)
            os.chmod(path, 0o600)
            return str(path)
        return None

    def _extract(self, url: str) -> dict:
        opts = {
            'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
            'socket_timeout': 15, 'noplaylist': True, 'retries': 1, 'extractor_retries': 1,
            'cachedir': False,
        }
        if self.cookie_path:
            opts['cookiefile'] = self.cookie_path
        # TLS 검사 유지. 구버전의 android/web 클라이언트 강제 지정 제거.
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict) or not info.get('url'):
            raise ValueError('오디오 주소를 가져오지 못했습니다.')
        if info.get('is_live'):
            raise ValueError('라이브 방송은 등장곡 구간 재생을 지원하지 않습니다.')
        if not str(info['url']).startswith('https://'):
            raise ValueError('안전한 오디오 주소가 아닙니다.')
        return info

    async def _extract_limited(self, url: str) -> dict:
        # 외부 timeout 시에도 스레드는 즉시 멈추지 않는다. 작업 종료까지 슬롯을 유지한다.
        async with self.extract_slots:
            return await asyncio.to_thread(self._extract, url)

    def stop(self, guild):
        self.generation[guild.id] += 1
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        self.now[guild.id] = {'title': '정지됨', 'status': 'stopped'}

    async def leave(self, guild):
        self.stop(guild)
        await self.voice.disconnect(guild)

    async def play(self, guild, team: str, song: dict, channel=None, order=None, preview=False):
        self.generation[guild.id] += 1
        generation = self.generation[guild.id]
        a, b = time_range(song.get('start', 0), song.get('end', 30))
        duration = min(5, b - a) if preview else b - a
        self.now[guild.id] = {'title': song.get('name', '등장곡'), 'status': 'preparing', 'team': team}
        try:
            if song.get('source', 'youtube') == 'upload':
                asset = self.assets.get(song['assetId'])
                if a >= asset['duration']:
                    raise ValueError('시작 시간이 오디오 길이를 초과합니다.')
                duration = min(duration, asset['duration'] - a)
                target = str(asset['path'])
                before = f'-nostdin -protocol_whitelist file,pipe -ss {a:.3f}'
            else:
                url = youtube_url(song.get('url'))
                task = asyncio.create_task(self._extract_limited(url))
                self.extract_tasks.add(task)
                def done(t):
                    self.extract_tasks.discard(t)
                    if not t.cancelled():
                        t.exception()  # timeout 이후의 백그라운드 실패도 회수한다.
                task.add_done_callback(done)
                try:
                    info = await asyncio.wait_for(asyncio.shield(task), timeout=50)
                except (asyncio.TimeoutError, yt_dlp.utils.DownloadError, ValueError) as exc:
                    log.warning('YouTube 추출 실패: %s', type(exc).__name__)
                    raise ValueError('YouTube 오디오를 가져오지 못했습니다. 영상 공개 여부·호스팅 IP 제한을 확인하세요. 직접 MP3 업로드로도 재생할 수 있습니다.') from exc
                target = info['url']
                if info.get('duration') and a >= float(info['duration']):
                    raise ValueError('시작 시간이 영상 길이를 초과합니다.')
                before = f'-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000 -ss {a:.3f}'
                # yt-dlp가 반환한 HTTP 헤더만 사용. 인수는 shell=False로 실행된다.
                headers = info.get('http_headers', {})
                ua = str(headers.get('User-Agent', '')).replace('\r', '').replace('\n', '')
                if ua:
                    before += ' -user_agent ' + shlex.quote(ua)
            if generation != self.generation[guild.id]:
                return False
            vc = await self.voice.connect(guild, channel)
            if generation != self.generation[guild.id]:
                return False
            if guild.me.voice and guild.me.voice.mute:
                raise VoiceError('봇이 서버 음소거 상태입니다. 통화방에서 봇의 서버 음소거를 해제하세요.')
            state = await self.store.state(team)
            if generation != self.generation[guild.id]:
                return False
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(target, executable=self.settings.ffmpeg, before_options=before, options=f'-vn -t {duration:.3f}'),
                volume=state['volume'],
            )
            self._start(guild, vc, source, generation, song.get('name', '등장곡'), team)
            if order is not None:
                await self.store.set_order(team, int(order))
            return True
        except Exception as exc:
            if generation == self.generation[guild.id]:
                self.now[guild.id] = {'title': song.get('name', '등장곡'), 'status': 'error', 'error': str(exc)[:300]}
            raise

    def _start(self, guild, vc, source, generation: int, title: str, team: str):
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        loop = asyncio.get_running_loop()
        def completed(error):
            def publish():
                if self.generation[guild.id] == generation:
                    self.now[guild.id] = {'title': title, 'team': team, 'status': 'error' if error else 'finished'}
                    if error:
                        self.now[guild.id]['error'] = '오디오 재생이 중단됐습니다. FFmpeg와 원본 파일/링크를 확인하세요.'
                        log.error('오디오 재생 오류: guild=%s (%s)', guild.id, type(error).__name__)
            if not loop.is_closed():
                loop.call_soon_threadsafe(publish)
        try:
            vc.play(source, after=completed)
        except BaseException:
            source.cleanup()
            raise
        self.now[guild.id] = {'title': title, 'team': team, 'status': 'playing'}

    async def event(self, guild, team: str, key: str, channel=None):
        self.generation[guild.id] += 1
        generation = self.generation[guild.id]
        custom = await self.store.event(team, key)
        if custom and custom.get('songName') and custom.get('category') == 'situation':
            song = await self.store.song(team, custom['songName'], 'situation')
            if not song:
                raise ValueError('연결된 상황별 노래가 없습니다. 경기 사운드에서 다시 선택하세요.')
            if generation != self.generation[guild.id]:
                return False
            return await self.play(guild, team, song, channel=channel)
        if custom and custom.get('assetId'):
            path = self.assets.get(custom['assetId'])['path']
        elif custom and custom.get('file'):
            path = self.assets.legacy_file(key, custom['file'])
        else:
            files = self.assets.bundled(key)
            if not files:
                raise ValueError('효과음 파일이 없습니다. 웹에서 오디오를 등록하세요.')
            path = random.choice(files)
        if generation != self.generation[guild.id]:
            return False
        vc = await self.voice.connect(guild, channel)
        state = await self.store.state(team)
        if generation != self.generation[guild.id]:
            return False
        if guild.me.voice and guild.me.voice.mute:
            raise VoiceError('봇의 서버 음소거를 해제하세요.')
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(str(path), executable=self.settings.ffmpeg, before_options='-nostdin -protocol_whitelist file,pipe', options='-vn'),
            volume=state['volume'],
        )
        self._start(guild, vc, source, generation, path.name, team)
        return True

    async def volume(self, guild, team: str, value: int):
        if not 0 <= value <= 100:
            raise ValueError('볼륨은 0~100입니다.')
        await self.store.set_volume(team, value / 100)
        vc = guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = value / 100

    async def close(self):
        if self.extract_tasks:
            await asyncio.gather(*self.extract_tasks, return_exceptions=True)
