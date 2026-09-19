"""봇과 같은 프로세스에서 실행하는 인증된 웹 관리 API."""
from __future__ import annotations
import hashlib
import hmac
import importlib.metadata
import json
import logging
import secrets
import shutil
import time
from collections import defaultdict, deque
from pathlib import Path
from aiohttp import web
from config import ROOT
from validation import EVENTS, clean_name, validate_song

log = logging.getLogger(__name__)
COOKIE = 'appearance_session'
SESSION_TTL = 8 * 60 * 60


def version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return '미설치'


def response(data: dict | list, status: int = 200):
    return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False))


async def body(request: web.Request) -> dict:
    if request.content_type != 'application/json':
        raise ValueError('JSON 요청이 필요합니다.')
    if request.content_length and request.content_length > 65536:
        raise ValueError('요청이 너무 큽니다.')
    raw = bytearray()
    async for chunk in request.content.iter_chunked(8192):
        raw.extend(chunk)
        if len(raw) > 65536:
            raise ValueError('요청이 너무 큽니다.')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('올바른 JSON 요청이 아닙니다.') from None
    if not isinstance(value, dict):
        raise ValueError('JSON 객체를 보내세요.')
    return value


class WebPanel:
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        self.store = bot.store
        self.assets = bot.assets
        self.sessions: dict[str, dict] = {}
        self.attempts: dict[str, deque] = defaultdict(deque)
        self.runner = None
        self.app = web.Application(middlewares=[self.security], client_max_size=(self.s.max_upload_mb + 1) * 1024 * 1024)
        self.app.add_routes([
            web.get('/', self.index),
            web.get('/assets/{name}', self.static),
            web.get('/healthz', self.health),
            web.post('/api/login', self.login),
            web.get('/api/session', self.session),
            web.post('/api/logout', self.logout),
            web.get('/api/state', self.state),
            web.post('/api/team', self.team),
            web.post('/api/songs', self.save_song),
            web.delete('/api/songs', self.delete_song),
            web.put('/api/lineup', self.save_lineup),
            web.post('/api/upload', self.upload),
            web.get('/api/media/{asset}', self.media),
            web.put('/api/events', self.event),
            web.post('/api/control', self.control),
            web.get('/api/diagnostics', self.diagnostics),
            web.get('/api/export', self.export),
        ])

    @web.middleware
    async def security(self, request, handler):
        try:
            if request.path.startswith('/api/'):
                if request.method not in {'GET', 'HEAD'}:
                    origin = request.headers.get('Origin')
                    allowed = {f'{request.scheme}://{request.host}'}
                    if self.s.public_url:
                        allowed.add(self.s.public_url)
                    if origin and origin not in allowed:
                        raise web.HTTPForbidden(text='다른 사이트에서 보낸 요청은 허용하지 않습니다.')
                if request.path != '/api/login':
                    key = request.cookies.get(COOKIE, '')
                    sess = self.sessions.get(key)
                    if not sess or sess['expires'] < time.monotonic():
                        self.sessions.pop(key, None)
                        raise web.HTTPUnauthorized(text='로그인이 필요합니다.')
                    request['session'] = sess
                    if request.method not in {'GET', 'HEAD'}:
                        csrf = request.headers.get('X-CSRF-Token', '')
                        if not hmac.compare_digest(csrf.encode(), sess['csrf'].encode()):
                            raise web.HTTPForbidden(text='보안 토큰이 만료됐습니다. 새로고침 후 다시 시도하세요.')
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = response({'error': exc.text if exc.status in {401, 403} else exc.reason}, exc.status)
        except (ValueError, KeyError, TypeError) as exc:
            resp = response({'error': str(exc)[:400]}, 400)
        except Exception as exc:
            # 토큰/쿠키/Firebase 키/yt-dlp 인증 헤더를 브라우저에 노출하지 않는다.
            log.exception('웹 요청 실패: %s %s', request.method, request.path)
            from_runtime = type(exc).__name__ == 'VoiceError'
            resp = response({'error': str(exc)[:400] if from_runtime else '처리하지 못했습니다. 서버 로그와 진단 탭을 확인하세요.'}, 503)
        resp.headers.update({
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'no-referrer',
            'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return resp

    async def index(self, request):
        return web.FileResponse(ROOT / 'static' / 'index.html')

    async def static(self, request):
        name = request.match_info['name']
        if name not in {'app.js', 'style.css'}:
            raise web.HTTPNotFound()
        return web.FileResponse(ROOT / 'static' / name)

    async def health(self, request):
        return response({'web': 'ok', 'discord': 'ready' if self.bot.is_ready() else 'offline'})

    async def login(self, request):
        if not self.s.web_password:
            raise web.HTTPForbidden(text='웹 비밀번호가 설정되지 않았습니다.')
        now = time.monotonic()
        # 프록시 전달 IP를 무조건 신뢰하지 않는다. 단일 프록시일 때 제한은 사용자끼리 공유된다.
        ip = request.remote or 'unknown'
        if len(self.attempts) > 2048:
            self.attempts = defaultdict(deque, {k: v for k, v in self.attempts.items() if v and now - v[-1] < 300})
        window = self.attempts[ip]
        while window and now - window[0] > 300:
            window.popleft()
        if len(window) >= 10:
            return response({'error': '로그인 시도가 많습니다. 5분 뒤에 다시 시도하세요.'}, 429)
        window.append(now)
        data = await body(request)
        supplied = str(data.get('password', ''))
        a = hashlib.sha256(supplied.encode()).digest()
        b = hashlib.sha256(self.s.web_password.encode()).digest()
        if not hmac.compare_digest(a, b):
            raise web.HTTPUnauthorized(text='비밀번호가 맞지 않습니다.')
        window.clear()
        self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
        if len(self.sessions) >= 256:
            oldest = min(self.sessions, key=lambda k: self.sessions[k]['expires'])
            self.sessions.pop(oldest)
        old = request.cookies.get(COOKIE)
        if old:
            self.sessions.pop(old, None)
        key, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        self.sessions[key] = {'expires': now + SESSION_TTL, 'csrf': csrf}
        resp = response({'ok': True, 'csrf': csrf})
        resp.set_cookie(COOKIE, key, httponly=True, secure=self.s.secure_cookie, samesite='Strict', max_age=SESSION_TTL, path='/')
        return resp

    async def session(self, request):
        return response({'csrf': request['session']['csrf']})

    async def logout(self, request):
        self.sessions.pop(request.cookies.get(COOKIE, ''), None)
        resp = response({'ok': True})
        resp.del_cookie(COOKIE, path='/')
        return resp

    def guild(self, guild_id):
        if not self.bot.is_ready():
            raise ValueError('봇이 Discord에 연결되어 있지 않습니다. 진단 탭을 확인하세요.')
        try:
            guild = self.bot.get_guild(int(guild_id))
        except (ValueError, TypeError):
            guild = None
        if guild is None:
            raise ValueError('서버를 선택하세요.')
        return guild

    async def state(self, request):
        teams = await self.store.teams()
        guilds = []
        for g in self.bot.guilds:
            guilds.append({'id': str(g.id), 'name': g.name})
        guild_id = request.query.get('guild_id')
        guild = self.bot.get_guild(int(guild_id)) if guild_id and guild_id.isdigit() else (self.bot.guilds[0] if self.bot.guilds else None)
        active = await self.store.get_team(guild.id) if guild else 'A팀'
        team = clean_name(request.query.get('team') or active, '팀 이름')
        if team not in teams:
            teams.append(team)
        state = await self.store.state(team)
        events = {e['id']: e for e in await self.store.events(team)}
        songs = await self.store.songs(team)
        for song in songs:
            if song.get('assetId'):
                try:
                    meta = self.assets.get(song['assetId'])
                    song['filename'] = meta['name']
                    song['fileDuration'] = meta['duration']
                except ValueError:
                    song['fileMissing'] = True
        channels = []
        if guild:
            for c in guild.voice_channels:
                perms = c.permissions_for(guild.me) if guild.me else None
                channels.append({'id': str(c.id), 'name': c.name, 'members': len([m for m in c.members if not m.bot]), 'available': bool(perms and perms.view_channel and perms.connect and perms.speak)})
        return response({
            'ready': self.bot.is_ready(), 'webOnly': self.s.web_only, 'storage': self.store.mode,
            'guilds': guilds, 'guildId': str(guild.id) if guild else None,
            'teams': teams, 'team': team, 'activeTeam': active, 'songs': songs,
            'lineup': await self.store.lineup(team), 'state': state, 'channels': channels,
            'voice': self.bot.player.voice.status(guild) if guild else None,
            'nowPlaying': self.bot.player.now.get(guild.id) if guild else None,
            'events': [{'key': k, 'label': v, 'custom': events.get(k), 'bundledCount': len(self.assets.bundled(k))} for k, v in EVENTS.items()],
            'maxUploadMb': self.s.max_upload_mb,
        })

    async def team(self, request):
        data = await body(request)
        team = clean_name(data.get('team'), '팀 이름')
        await self.store.create_team(team)
        if data.get('activate'):
            guild = self.guild(data.get('guildId'))
            await self.store.set_team(guild.id, team)
        return response({'ok': True})

    async def save_song(self, request):
        data = await body(request)
        team = clean_name(data.get('team'), '팀 이름')
        song = validate_song(data)
        if song['source'] == 'upload':
            meta = self.assets.get(song['assetId'])
            if song['start'] >= meta['duration'] or song['end'] > meta['duration'] + .25:
                raise ValueError(f'재생 구간이 파일 길이({meta["duration"]:.1f}초)를 초과합니다.')
        await self.store.save_song(team, song)
        return response({'ok': True})

    async def delete_song(self, request):
        await self.store.delete_song(clean_name(request.query.get('team')), clean_name(request.query.get('name')))
        return response({'ok': True})

    async def save_lineup(self, request):
        data = await body(request)
        values = data.get('lineup')
        if not isinstance(values, dict):
            raise ValueError('타순 데이터가 올바르지 않습니다.')
        await self.store.save_lineup(clean_name(data.get('team')), values)
        return response({'ok': True})

    async def upload(self, request):
        if request.content_type != 'multipart/form-data':
            raise ValueError('파일 업로드 형식이 올바르지 않습니다.')
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != 'file' or not part.filename:
            raise ValueError('오디오 파일을 선택하세요.')
        result = await self.assets.upload(part)
        return response(result)

    async def media(self, request):
        meta = self.assets.get(request.match_info['asset'])
        resp = web.FileResponse(meta['path'])
        resp.headers['Content-Disposition'] = 'inline'
        return resp

    async def event(self, request):
        data = await body(request)
        team, key = clean_name(data.get('team')), str(data.get('key', ''))
        if key not in EVENTS:
            raise ValueError('알 수 없는 효과음입니다.')
        asset = data.get('assetId')
        if asset:
            meta = self.assets.get(str(asset))
            await self.store.set_event(team, key, {'assetId': meta['id'], 'filename': meta['name']})
        else:
            await self.store.set_event(team, key, None)
        return response({'ok': True})

    async def control(self, request):
        d = await body(request)
        guild = self.guild(d.get('guildId'))
        team = clean_name(d.get('team') or await self.store.get_team(guild.id))
        action = d.get('action')
        channel_id = d.get('channelId') if action in {'connect', 'play', 'order', 'next', 'event'} else None
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel_id and channel is None:
            raise ValueError('선택한 통화방이 없습니다.')
        player = self.bot.player
        if action == 'connect':
            await player.voice.connect(guild, channel)
        elif action == 'disconnect':
            await player.leave(guild)
        elif action == 'stop':
            player.stop(guild)
        elif action == 'volume':
            await player.volume(guild, team, int(d.get('value', -1)))
        elif action == 'play':
            song = await self.store.song(team, clean_name(d.get('name')))
            if not song:
                raise ValueError('등록된 곡이 없습니다.')
            await player.play(guild, team, song, channel=channel, preview=bool(d.get('preview')))
        elif action in {'order', 'next'}:
            state = await self.store.state(team)
            order = (state['currentOrder'] % 9) + 1 if action == 'next' else int(d.get('order', 0))
            if not 1 <= order <= 9:
                raise ValueError('타순은 1~9번입니다.')
            name = (await self.store.lineup(team)).get(str(order))
            song = await self.store.song(team, name) if name else None
            if not song:
                raise ValueError(f'{order}번 타자/등장곡을 먼저 등록하세요.')
            await player.play(guild, team, song, channel=channel, order=order)
        elif action == 'event':
            key = str(d.get('key', ''))
            if key not in EVENTS:
                raise ValueError('알 수 없는 효과음입니다.')
            await player.event(guild, team, key, channel)
        else:
            raise ValueError('알 수 없는 명령입니다.')
        manage_idle = getattr(self.bot, 'manage_idle', None)
        if manage_idle:
            await manage_idle(guild)
        if action in {'order', 'next'} and hasattr(self.bot, 'refresh_lineup'):
            await self.bot.refresh_lineup(guild)
        return response({'ok': True, 'voice': player.voice.status(guild), 'nowPlaying': player.now.get(guild.id)})

    async def diagnostics(self, request):
        return response({
            'discordReady': self.bot.is_ready(), 'webOnly': self.s.web_only,
            'storage': self.store.mode,
            'packages': {p: version(p) for p in ['discord.py', 'PyNaCl', 'davey', 'yt-dlp', 'yt-dlp-ejs', 'aiohttp', 'firebase-admin']},
            'ffmpeg': bool(shutil.which(self.s.ffmpeg)), 'ffprobe': bool(shutil.which(self.s.ffprobe)),
            'deno': bool(shutil.which('deno')),
            'cookiesConfigured': bool(self.s.cookies_path or self.s.cookies_b64),
            'secureCookie': self.s.secure_cookie, 'maxUploadMb': self.s.max_upload_mb,
            'maxStorageMb': self.s.max_storage_mb,
            'checks': [
                'Discord Developer Portal → Bot → Message Content Intent 켜기 (!명령어용)',
                '통화방 권한 덮어쓰기에서 채널 보기·연결·말하기 허용',
                '서버 음소거 해제, 통화방 인원 제한 확인',
                '같은 DISCORD_TOKEN으로 봇을 두 곳에서 실행하지 않기 (replica=1)',
                '음성 연결은 UDP 송수신이 필요함. HTTP 포트만 열어서는 해결되지 않음',
                'YouTube 링크 재생에는 Deno와 yt-dlp-ejs도 필요함. Docker 이미지에 포함',
                'YouTube 차단은 음성 연결 오류와 별개. MP3 업로드로 구분 테스트',
                '업로드 파일과 로컬 DB는 DATA_DIR 영구 저장소에 보관',
            ],
        })

    async def export(self, request):
        resp = response(await self.store.export())
        resp.headers['Content-Disposition'] = 'attachment; filename="song-metadata-backup.json"'
        return resp

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.s.host, self.s.port).start()
        log.info('웹 관리 화면 시작 (port=%s). 비밀번호와 토큰은 공유하지 마세요.', self.s.port)

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
