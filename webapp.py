"""봇과 같은 프로세스에서 실행하는 인증된 웹 관리 API."""
from __future__ import annotations
import hashlib
import asyncio
import tempfile
from urllib.parse import quote
import hmac
import importlib.metadata
import json
import logging
import secrets
import base64
import os
import re
import shutil
import time
from collections import defaultdict, deque
from pathlib import Path
from aiohttp import web
from config import ROOT, normalize_origin
from validation import EVENTS, clean_name, validate_song, song_category, SONG_CATEGORIES, volume_percent, saved_volume_percent
from persistence import storage_status
from backups import write_music_backup
from permissions import allowed, required_capability, ROLE_LABELS, ROLE_CAPABILITIES
from event_tracks import event_tracks, MAX_EVENT_TRACKS

log = logging.getLogger(__name__)
COOKIE = 'appearance_session'
SESSION_TTL = 8 * 60 * 60
USERNAME_RE = re.compile(r'^[a-z0-9_.-]{3,24}$')
PBKDF2_ROUNDS = 310_000

def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PBKDF2_ROUNDS)
    return f'pbkdf2_sha256${PBKDF2_ROUNDS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(dk).decode()}'

def password_ok(password: str, encoded: str) -> bool:
    try:
        alg, rounds, salt64, expected64 = encoded.split('$', 3)
        if alg != 'pbkdf2_sha256': return False
        salt=base64.urlsafe_b64decode(salt64); expected=base64.urlsafe_b64decode(expected64)
        actual=hashlib.pbkdf2_hmac('sha256', password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except Exception: return False
CORS_METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS'}
CORS_HEADERS = {'authorization', 'content-type', 'x-csrf-token', 'range'}


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
        self.backup_lock = asyncio.Lock()
        self.account_lock = asyncio.Lock()
        # Firestore free-tier 보호: /api/state의 DB-backed 부분을 짧게 캐시합니다.
        # 실시간 음성 상태는 /api/live에서 별도로 갱신하므로 이 캐시는 음악/타순/상황 목록에만 영향합니다.
        self.state_cache: dict[tuple[str, str, str], dict] = {}
        self.state_cache_ttl = 300.0
        self.app = web.Application(middlewares=[self.cors, self.security], client_max_size=(self.s.max_upload_mb + 1) * 1024 * 1024)
        self.app.add_routes([
            web.get('/', self.index),
            web.get('/assets/{name}', self.static),
            web.get('/{name:app\\.js|config\\.js|style\\.css}', self.static),
            web.get('/{name:download\\.html|offline\\.html|manifest\\.webmanifest|sw\\.js|pwa\\.js}', self.static),
            web.get('/icons/{name}', self.icon),
            web.get('/healthz', self.health),
            web.get('/api/connection', self.connection),
            web.post('/api/login', self.login),
            web.post('/api/signup', self.signup),
            web.get('/api/users', self.users),
            web.put('/api/users/{username}', self.update_user),
            web.delete('/api/users/{username}', self.remove_user),
            web.get('/api/session', self.session),
            web.post('/api/logout', self.logout),
            web.get('/api/state', self.state),
            web.get('/api/live', self.live),
            web.get('/api/playback-settings', self.playback_settings),
            web.put('/api/playback-settings', self.playback_settings),
            web.post('/api/team', self.team),
            web.post('/api/songs', self.save_song),
            web.delete('/api/songs', self.delete_song),
            web.put('/api/lineup', self.save_lineup),
            web.post('/api/upload', self.upload),
            web.get('/api/media/{asset}', self.media),
            web.post('/api/events', self.create_event),
            web.put('/api/events', self.event),
            web.delete('/api/events', self.delete_event),
            web.post('/api/control', self.control),
            web.get('/api/diagnostics', self.diagnostics),
            web.get('/api/export', self.export),
            web.get('/api/backup', self.backup),
        ])

    def request_origin(self, request):
        # Browsers omit Origin on same-origin GET, but include it on CORS requests.
        return request.headers.get('Origin') or self.s.public_url or f'{request.scheme}://{request.host}'

    def allowed_origins(self, request):
        allowed = set(self.s.web_origins)
        for value in (self.s.public_url, self.s.web_url, f'{request.scheme}://{request.host}'):
            if value:
                try:
                    allowed.add(normalize_origin(value))
                except ValueError:
                    pass
        return allowed

    @web.middleware
    async def cors(self, request, handler):
        """CORS wraps security so authentication failures are readable by our UI."""
        protected_path = request.path.startswith('/api/') or request.path == '/healthz'
        origin = request.headers.get('Origin', '')
        allowed = self.allowed_origins(request)
        accepted = bool(origin and origin in allowed)
        if protected_path and origin and not accepted:
            resp = response({'error': '이 웹사이트 주소가 허용되지 않았습니다. Railway의 WEB_ORIGINS에 현재 Firebase 주소를 추가하고 재배포하세요.'}, 403)
        elif protected_path and request.method == 'OPTIONS':
            method = request.headers.get('Access-Control-Request-Method', '').upper()
            headers = {h.strip().lower() for h in request.headers.get('Access-Control-Request-Headers', '').split(',') if h.strip()}
            if not accepted or method not in CORS_METHODS or not headers.issubset(CORS_HEADERS):
                resp = response({'error': '허용되지 않은 사전 요청입니다.'}, 403)
            else:
                resp = web.Response(status=204)
                resp.headers['Access-Control-Allow-Methods'] = ', '.join(sorted(CORS_METHODS))
                resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-CSRF-Token, Range'
                resp.headers['Access-Control-Max-Age'] = '600'
        else:
            resp = await handler(request)
        if protected_path:
            resp.headers['Vary'] = 'Origin'
            resp.headers['Cache-Control'] = 'no-store'
            resp.headers['X-Content-Type-Options'] = 'nosniff'
            if accepted:
                resp.headers['Access-Control-Allow-Origin'] = origin
                resp.headers['Access-Control-Expose-Headers'] = 'Content-Disposition, Content-Range, Accept-Ranges'
                # No cross-site cookies: browser clients use Authorization: Bearer.
        return resp

    @web.middleware
    async def security(self, request, handler):
        try:
            if request.path.startswith('/api/') and request.path not in {'/api/login', '/api/signup', '/api/connection'}:
                authorization = request.headers.get('Authorization', '')
                if authorization:
                    parts = authorization.split()
                    if len(parts) != 2 or parts[0].lower() != 'bearer':
                        raise web.HTTPUnauthorized(text='로그인 토큰이 올바르지 않습니다.')
                    key, mode = parts[1], 'bearer'
                else:
                    key, mode = request.cookies.get(COOKIE, ''), 'cookie'
                sess = self.sessions.get(key)
                if not sess or sess['expires'] < time.monotonic():
                    self.sessions.pop(key, None)
                    raise web.HTTPUnauthorized(text='로그인이 필요합니다. 다시 로그인하세요.')
                if sess.get('mode', 'cookie') != mode:
                    raise web.HTTPUnauthorized(text='로그인 방식이 맞지 않습니다. 다시 로그인하세요.')
                # A browser token obtained by one website cannot be used by another origin.
                if mode == 'bearer' and sess.get('origin') != self.request_origin(request):
                    raise web.HTTPForbidden(text='로그인한 웹사이트에서 다시 시도하세요.')
                request['session'], request['session_key'] = sess, key
                role = sess.get('role', 'pending')
                if not allowed(role, 'read'):
                    raise web.HTTPForbidden(text='관리자 승인 대기 중인 계정입니다.')
                capability = required_capability(request.path, request.method)
                if not allowed(role, capability):
                    raise web.HTTPForbidden(text='이 기능을 사용할 권한이 없습니다. 등록/삭제는 등록/삭제자, 재생 제어는 재생자 이상 권한이 필요합니다.')
                if request.method not in {'GET', 'HEAD'}:
                    csrf = request.headers.get('X-CSRF-Token', '')
                    if not hmac.compare_digest(csrf.encode(), sess['csrf'].encode()):
                        raise web.HTTPForbidden(text='보안 토큰이 만료됐습니다. 새로고침 후 다시 시도하세요.')
            resp = await handler(request)
            if request.path.startswith('/api/') and request.method not in {'GET', 'HEAD', 'OPTIONS'} and getattr(resp, 'status', 200) < 400:
                # 웹에서 저장/수정/삭제/재생 제어를 한 직후에는 오래된 DB 캐시를 쓰지 않는다.
                self.state_cache.clear()
        except web.HTTPException as exc:
            resp = response({'error': exc.text if exc.status in {401, 403} else exc.reason}, exc.status)
        except (ValueError, KeyError, TypeError) as exc:
            resp = response({'error': str(exc)[:400]}, 400)
        except Exception as exc:
            # Firestore 무료 할당량 소진은 웹/봇 프로세스 장애가 아니다. 사용자에게 원인을 명확히 표시한다.
            message = str(exc)
            quota_error = ('Quota exceeded' in message or 'RESOURCE_EXHAUSTED' in message
                           or type(exc).__name__ in {'ResourceExhausted', 'RetryError'})
            if quota_error:
                log.warning('Firestore quota exceeded: %s %s', request.method, request.path)
                resp = response({'error': 'Firebase Firestore 읽기 할당량을 초과했습니다. 할당량이 갱신된 뒤 다시 시도하세요. 최신 패치는 실시간 화면 갱신에서 Firestore를 반복 조회하지 않습니다.'}, 429)
            else:
                # 토큰/쿠키/Firebase 키/yt-dlp 인증 헤더를 브라우저에 노출하지 않는다.
                log.exception('웹 요청 실패: %s %s', request.method, request.path)
                from_runtime = type(exc).__name__ == 'VoiceError'
                resp = response({'error': str(exc)[:400] if from_runtime else '처리하지 못했습니다. 서버 로그와 진단 탭을 확인하세요.'}, 503)
        resp.headers.update({
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'no-referrer',
            'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return resp

    async def index(self, request):
        return web.FileResponse(ROOT / 'static' / 'index.html')

    async def static(self, request):
        name = request.match_info['name']
        if name not in {'app.js', 'style.css', 'config.js', 'pwa.js', 'sw.js', 'manifest.webmanifest', 'download.html', 'offline.html'}:
            raise web.HTTPNotFound()
        resp = web.FileResponse(ROOT / 'static' / name)
        if name == 'manifest.webmanifest': resp.content_type = 'application/manifest+json'
        if name == 'sw.js': resp.headers['Service-Worker-Allowed'] = '/'
        return resp

    async def icon(self, request):
        name = request.match_info['name']
        if name not in {'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'}:
            raise web.HTTPNotFound()
        return web.FileResponse(ROOT / 'static' / 'icons' / name)

    def all_bots(self):
        bots = [self.bot]
        peer = getattr(self.bot, 'peer_bot', None)
        if peer is not None:
            bots.append(peer)
        return bots

    def bot_slot(self, bot) -> str:
        return 'secondary' if getattr(bot, 'slot', 'primary') == 'secondary' else 'primary'

    def bot_label(self, bot) -> str:
        slot = self.bot_slot(bot)
        default = (getattr(self.s, 'secondary_bot_label', '백팀 봇') if slot == 'secondary'
                   else getattr(self.s, 'primary_bot_label', '청팀 봇'))
        return str(getattr(bot, 'label', default) or default)

    def bot_targets(self):
        rows = []
        for bot in self.all_bots():
            user = getattr(bot, 'user', None)
            rows.append({
                'id': self.bot_slot(bot),
                'name': self.bot_label(bot),
                'ready': bool(bot.is_ready()),
                'userId': str(user.id) if user else None,
                'username': str(user) if user else None,
            })
        if getattr(self.bot.settings, 'secondary_token', '') and not any(x['id'] == 'secondary' for x in rows):
            rows.append({'id': 'secondary', 'name': getattr(self.s, 'secondary_bot_label', '백팀 봇'),
                         'ready': False, 'userId': None, 'username': None})
        return rows

    def target_bot(self, target: str | None):
        target = 'secondary' if str(target or 'primary').lower() == 'secondary' else 'primary'
        if target == 'primary':
            return self.bot
        peer = getattr(self.bot, 'peer_bot', None)
        if peer is None:
            raise ValueError('보조 봇이 실행되지 않았습니다. Railway에 DISCORD_TOKEN_SECONDARY를 설정하고 재배포하세요.')
        return peer

    async def health(self, request):
        bots = self.bot_targets()
        return response({'web': 'ok', 'discord': 'ready' if self.bot.is_ready() else 'offline',
                         'bots': bots})

    async def connection(self, request):
        # Safe, unauthenticated endpoint: checks API identity + CORS, not just a 200 page.
        bots = self.bot_targets()
        return response({'service': 'appearance-song-bot', 'apiVersion': 2,
                         'authMode': 'bearer', 'web': 'ok', 'build': 'song-volume-20260926-1',
                         'capabilities': ['song-volume', 'event-track-volume', 'library-categories', 'atomic-rename', 'storage-check',
                                          'roles-v3', 'global-autoplay', 'stage-voice', 'event-all-categories', 'event-delete-restore', 'original-name-backup', 'pwa', 'dual-bot', 'per-bot-team', 'event-play-modes', 'separate-voice-channels', 'railway-sqlite'],
                         'discordReady': self.bot.is_ready(), 'secondaryReady': any(
                             x['id'] == 'secondary' and x['ready'] for x in bots),
                         'bots': bots, 'webOnly': self.s.web_only})

    async def _ensure_admin(self):
        async with self.account_lock:
            if self.s.web_password and await self.store.web_user('admin') is None:
                hashed = await asyncio.to_thread(password_hash, self.s.web_password)
                await self.store.save_web_user('admin', {'passwordHash': hashed, 'displayName':'관리자',
                    'role':'admin', 'enabled':True, 'createdAt':int(time.time())})

    def _limit_login(self, request):
        now=time.monotonic(); ip=request.remote or 'unknown'; window=self.attempts[ip]
        while window and now-window[0]>300: window.popleft()
        if len(window)>=10: raise web.HTTPTooManyRequests(text='로그인 시도가 많습니다. 5분 뒤에 다시 시도하세요.')
        window.append(now); return now, window

    async def signup(self, request):
        await self._ensure_admin(); d=await body(request)
        username=str(d.get('username','')).strip().lower(); display=str(d.get('displayName','')).strip(); password=str(d.get('password',''))
        if not USERNAME_RE.fullmatch(username) or username=='admin': raise ValueError('아이디는 영문 소문자, 숫자, _, -, . 조합 3~24자로 입력하세요.')
        if not 2<=len(display)<=30: raise ValueError('표시 이름은 2~30자로 입력하세요.')
        if not 10<=len(password)<=128: raise ValueError('비밀번호는 10자 이상으로 입력하세요.')
        async with self.account_lock:
            if await self.store.web_user(username): raise ValueError('이미 사용 중인 아이디입니다.')
            hashed = await asyncio.to_thread(password_hash, password)
            await self.store.save_web_user(username, {'passwordHash':hashed,'displayName':display,'role':'pending','enabled':True,'createdAt':int(time.time())})
        return response({'ok':True,'message':'가입 신청 완료! 관리자가 유저·재생자·등록/삭제자 중 하나로 승인하면 로그인할 수 있습니다.'},201)

    async def login(self, request):
        await self._ensure_admin(); now,window=self._limit_login(request); d=await body(request)
        mode=d.get('authMode','cookie'); username=str(d.get('username') or 'admin').strip().lower(); supplied=str(d.get('password',''))
        if mode not in {'cookie','bearer'}: raise ValueError('지원하지 않는 로그인 방식입니다.')
        user=await self.store.web_user(username)
        if not user or not await asyncio.to_thread(password_ok,supplied,user.get('passwordHash','')): raise web.HTTPUnauthorized(text='아이디 또는 비밀번호가 맞지 않습니다.')
        if not user.get('enabled',True): raise web.HTTPForbidden(text='사용이 중지된 계정입니다.')
        if not allowed(user.get('role'), 'read'): raise web.HTTPForbidden(text='관리자 승인 대기 중입니다.')
        window.clear(); key,csrf=secrets.token_urlsafe(40),secrets.token_urlsafe(32)
        sess={'expires':now+SESSION_TTL,'csrf':csrf,'mode':mode,'origin':self.request_origin(request),'username':username,'displayName':user.get('displayName',username),'role':user.get('role')}
        self.sessions[key]=sess; payload={'ok':True,'csrf':csrf,'user':{'username':username,'displayName':sess['displayName'],'role':sess['role']}}
        if mode=='bearer': payload.update({'accessToken':key,'expiresIn':SESSION_TTL}); return response(payload)
        resp=response(payload); resp.set_cookie(COOKIE,key,httponly=True,secure=self.s.secure_cookie,samesite='Strict',max_age=SESSION_TTL,path='/'); return resp

    async def users(self, request):
        rows=await self.store.web_users(); return response({'users':[{k:v for k,v in u.items() if k!='passwordHash'} for u in rows]})

    async def update_user(self, request):
        username=request.match_info['username'].strip().lower()
        if username=='admin': raise ValueError('기본 관리자 계정 권한은 변경할 수 없습니다.')
        user=await self.store.web_user(username)
        if not user: raise ValueError('계정을 찾을 수 없습니다.')
        d=await body(request); role=d.get('role',user.get('role','pending'))
        if role not in {'pending','registrar','player','user'}: raise ValueError('승인 대기, 등장곡 등록/삭제자, 등장곡 재생자, 유저 중에서 선택하세요.')
        await self.store.save_web_user(username,{'role':role,'enabled':bool(d.get('enabled',user.get('enabled',True)))},True)
        self.sessions = {k:v for k,v in self.sessions.items() if v.get('username') != username}
        return response({'ok':True})

    async def remove_user(self, request):
        username=request.match_info['username'].strip().lower()
        if username=='admin': raise ValueError('기본 관리자 계정은 삭제할 수 없습니다.')
        await self.store.delete_web_user(username); self.sessions={k:v for k,v in self.sessions.items() if v.get('username')!=username}; return response({'ok':True})

    async def session(self, request):
        return response({'csrf':request['session']['csrf'],'user':{k:request['session'].get(k) for k in ('username','displayName','role')}, 'capabilities': sorted(ROLE_CAPABILITIES.get(request['session'].get('role'), ()))})

    async def logout(self, request):
        self.sessions.pop(request['session_key'], None)
        resp = response({'ok': True})
        resp.del_cookie(COOKIE, path='/')
        return resp

    def guild(self, guild_id, target: str = 'primary'):
        bot = self.target_bot(target)
        if not bot.is_ready():
            raise ValueError(f'{self.bot_label(bot)}이 Discord에 연결되어 있지 않습니다. 진단 탭을 확인하세요.')
        try:
            guild = bot.get_guild(int(guild_id))
        except (ValueError, TypeError):
            guild = None
        if guild is None:
            raise ValueError(f'{self.bot_label(bot)}이 들어가 있는 Discord 서버를 선택하세요.')
        return guild

    def _live_payload(self, target_bot, guild, target: str) -> dict:
        peer_voice_id = None
        peer_voice_name = None
        peer_label = None
        if guild:
            peer = getattr(target_bot, 'peer_bot', None)
            if peer is not None:
                peer_guild = peer.get_guild(guild.id)
                peer_vc = peer_guild.voice_client if peer_guild else None
                if peer_vc and peer_vc.channel:
                    peer_voice_id = str(peer_vc.channel.id)
                    peer_voice_name = peer_vc.channel.name
                    peer_label = self.bot_label(peer)
        return {
            'ready': target_bot.is_ready(),
            'botTarget': target,
            'botLabel': self.bot_label(target_bot),
            'botTargets': self.bot_targets(),
            'guildId': str(guild.id) if guild else None,
            'voice': target_bot.player.voice.status(guild) if guild else None,
            'otherBotVoice': ({'channelId': peer_voice_id, 'channelName': peer_voice_name, 'botLabel': peer_label}
                              if peer_voice_id else None),
            'nowPlaying': target_bot.player.now.get(guild.id) if guild else None,
            'autoEntrance': self.store.playback_snapshot(),
        }

    async def live(self, request):
        """Discord 실시간 상태 전용. Firestore를 전혀 읽지 않는다."""
        target = 'secondary' if request.query.get('bot_target') == 'secondary' else 'primary'
        target_bot = self.target_bot(target)
        guild_id = request.query.get('guild_id')
        guild = target_bot.get_guild(int(guild_id)) if guild_id and guild_id.isdigit() else (target_bot.guilds[0] if target_bot.guilds else None)
        return response(self._live_payload(target_bot, guild, target))

    async def state(self, request):
        await self.store.playback_settings()
        target = 'secondary' if request.query.get('bot_target') == 'secondary' else 'primary'
        target_bot = self.target_bot(target)
        guilds = [{'id': str(g.id), 'name': g.name} for g in target_bot.guilds]
        guild_id = request.query.get('guild_id')
        guild = target_bot.get_guild(int(guild_id)) if guild_id and guild_id.isdigit() else (target_bot.guilds[0] if target_bot.guilds else None)
        requested_team = request.query.get('team') or ''
        cache_key = (target, str(guild.id) if guild else '', requested_team)
        now = time.monotonic()
        cached = self.state_cache.get(cache_key)
        if cached and now - cached['at'] < self.state_cache_ttl:
            static = dict(cached['data'])
        else:
            teams = await self.store.teams()
            active = await self.store.get_team(guild.id, target) if guild else 'A팀'
            team = clean_name(requested_team or active, '팀 이름')
            if team not in teams:
                teams.append(team)
            state = await self.store.state(team)
            events = {e['id']: e for e in await self.store.events(team)}
            library = await self.store.library(team)
            for songs in library.values():
                for song in songs:
                    if song.get('assetId'):
                        try:
                            meta = await asyncio.to_thread(self.assets.get, song['assetId'])
                            song['filename'] = meta['name']
                            song['fileDuration'] = meta['duration']
                        except (ValueError, OSError):
                            # Preserve the record and ID. Never hide/delete a missing audio row.
                            song['fileMissing'] = True
            static = {
                'webOnly': self.s.web_only,
                'storage': self.store.mode,
                'teams': teams,
                'team': team,
                'activeTeam': active,
                'songs': library['entrance'],
                'library': library,
                'categories': SONG_CATEGORIES,
                'fileStorage': storage_status(self.s),
                'lineup': await self.store.lineup(team),
                'state': state,
                'events': self._event_rows(events),
                'deletedEvents': [{'key': k, 'label': d.get('label') or EVENTS.get(k, k)} for k, d in events.items() if d.get('deleted')],
                'songVolumeEnabled': True,
                'maxEventTracks': MAX_EVENT_TRACKS,
                'maxUploadMb': self.s.max_upload_mb,
            }
            self.state_cache[cache_key] = {'at': now, 'data': static}
            # 방치된 키가 계속 늘지 않게 작게 제한한다.
            if len(self.state_cache) > 64:
                oldest = min(self.state_cache, key=lambda k: self.state_cache[k]['at'])
                self.state_cache.pop(oldest, None)

        channels = []
        live = self._live_payload(target_bot, guild, target)
        peer_voice_id = (live.get('otherBotVoice') or {}).get('channelId')
        peer_label = (live.get('otherBotVoice') or {}).get('botLabel')
        if guild:
            for c in [*guild.voice_channels, *getattr(guild, 'stage_channels', [])]:
                perms = c.permissions_for(guild.me) if guild.me else None
                occupied = peer_voice_id == str(c.id)
                is_stage = c in getattr(guild, 'stage_channels', [])
                channels.append({'id': str(c.id), 'name': c.name, 'type': 'stage' if is_stage else 'voice',
                                 'stageAutoSpeaker': bool(perms and getattr(perms, 'mute_members', False)) if is_stage else None,
                                 'members': len([m for m in c.members if not m.bot]),
                                 'occupiedByOtherBot': occupied,
                                 'occupiedByLabel': peer_label if occupied else None,
                                 'available': bool(perms and perms.view_channel and perms.connect and (is_stage or perms.speak)) and not occupied})
        payload = dict(static)
        payload.update(live)
        payload.update({'guilds': guilds, 'channels': channels})
        return response(payload)

    def _event_tracks(self, doc: dict | None) -> list[dict]:
        return event_tracks(doc)

    async def _validated_event_tracks(self, team: str, value, previous: dict | None = None) -> list[dict]:
        if not isinstance(value, list):
            raise ValueError('경기 상황 곡 목록이 올바르지 않습니다.')
        if len(value) > MAX_EVENT_TRACKS:
            raise ValueError(f'한 경기 상황에는 최대 {MAX_EVENT_TRACKS}곡까지 넣을 수 있습니다.')
        result, seen = [], set()
        old_assets = {t['assetId']: t for t in self._event_tracks(previous) if t['type'] == 'asset'}
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError('경기 상황 곡 정보가 올바르지 않습니다.')
            kind = str(raw.get('type', ''))
            if kind == 'song':
                if 'volumePercent' in raw:
                    raise ValueError('라이브러리 곡의 볼륨은 음악 라이브러리에서 수정하세요. 연결에는 중복 적용하지 않습니다.')
                name = clean_name(raw.get('songName'), '곡 이름')
                category = song_category(raw.get('category', 'situation'))
                if not await self.store.song(team, name, category):
                    raise ValueError(f'{SONG_CATEGORIES[category]} 라이브러리에 “{name}”을 먼저 등록하세요.')
                ident = ('song', category, name)
                item = {'type': 'song', 'songName': name, 'category': category}
            elif kind == 'asset':
                meta = self.assets.get(str(raw.get('assetId', '')))
                ident = ('asset', meta['id'])
                percent = (volume_percent(raw['volumePercent']) if 'volumePercent' in raw
                           else saved_volume_percent(old_assets.get(meta['id'])))
                item = {'type': 'asset', 'assetId': meta['id'], 'filename': meta['name'], 'volumePercent': percent}
            else:
                raise ValueError('경기 상황에는 등장곡·응원가·상황별 노래 또는 업로드 오디오를 넣을 수 있습니다.')
            if ident in seen:
                continue
            seen.add(ident); result.append(item)
        return result

    def _event_rows(self, events: dict[str, dict]) -> list[dict]:
        rows = []
        for key, label in EVENTS.items():
            doc = events.get(key)
            if doc and doc.get('deleted'): continue
            source = None
            if doc:
                source = {k: v for k, v in doc.items() if k not in {'id', 'label', 'isCustom'}} or None
                if source is not None:
                    source['tracks'] = self._event_tracks(doc)
                    source['playMode'] = str(doc.get('playMode') or 'single')
            rows.append({'key': key, 'label': label, 'custom': source,
                         'customEvent': False, 'bundledCount': len(self.assets.bundled(key))})
        extras = []
        for key, doc in events.items():
            if key in EVENTS or not doc.get('isCustom') or doc.get('deleted'):
                continue
            label = str(doc.get('label') or key)
            source = {k: v for k, v in doc.items() if k not in {'id', 'label', 'isCustom'}} or None
            if source is not None:
                source['tracks'] = self._event_tracks(doc)
                source['playMode'] = str(doc.get('playMode') or 'single')
            extras.append({'key': key, 'label': label, 'custom': source,
                           'customEvent': True, 'bundledCount': 0})
        extras.sort(key=lambda row: row['label'].casefold())
        return rows + extras

    async def create_event(self, request):
        data = await body(request)
        team = clean_name(data.get('team'), '팀 이름')
        label = clean_name(data.get('label'), '상황 이름')
        existing = await self.store.events(team)
        labels = {str(x.get('label') or EVENTS.get(x.get('id'), '')).casefold() for x in existing}
        labels.update(v.casefold() for v in EVENTS.values())
        if label.casefold() in labels:
            raise ValueError('같은 이름의 경기 상황이 이미 있습니다.')
        for _ in range(10):
            key = 'custom_' + secrets.token_hex(6)
            if not await self.store.event(team, key):
                break
        else:
            raise RuntimeError('경기 상황 ID를 만들지 못했습니다.')
        mode = str(data.get('playMode') or 'single')
        if mode not in {'single', 'random', 'sequence'}:
            raise ValueError('재생 방식이 올바르지 않습니다.')
        await self.store.set_event(team, key, {'label': label, 'isCustom': True, 'playMode': mode, 'tracks': []})
        return response({'ok': True, 'key': key, 'label': label, 'playMode': mode}, 201)

    async def delete_event(self, request):
        team = clean_name(request.query.get('team'), '팀 이름')
        key = str(request.query.get('key', ''))
        current = await self.store.event(team, key)
        if key not in EVENTS and not (current and current.get('isCustom')):
            raise ValueError('알 수 없는 경기 상황입니다.')
        # Tombstones remove built-ins too without erasing bundled files or breaking another team.
        await self.store.set_event(team, key, {**(current or {}), 'deleted': True})
        return response({'ok': True})

    async def playback_settings(self, request):
        if request.method == 'PUT':
            data = await body(request)
            value = await self.store.set_auto_entrance(data.get('enabled'))
        else:
            value = await self.store.playback_settings()
        return response({'ok': True, 'autoEntrance': value})

    async def team(self, request):
        data = await body(request)
        team = clean_name(data.get('team'), '팀 이름')
        await self.store.create_team(team)
        if data.get('activate'):
            target = 'secondary' if data.get('botTarget') == 'secondary' else 'primary'
            guild = self.guild(data.get('guildId'), target)
            await self.store.set_team(guild.id, team, target)
        return response({'ok': True})

    async def save_song(self, request):
        data = await body(request)
        team = clean_name(data.get('team'), '팀 이름')
        song = validate_song(data)
        old_name = clean_name(data['oldName']) if data.get('oldName') is not None else None
        old = await self.store.song(team, old_name, song['category']) if old_name else None
        warning = ''
        if song['source'] == 'upload':
            try:
                meta = await asyncio.to_thread(self.assets.get, song['assetId'])
            except ValueError:
                # Renaming a missing-file record must not force the user to lose its
                # asset ID, nickname link or lineup. Playback still reports missing audio.
                same_audio = old and all(old.get(k) == song.get(k) for k in ('source', 'assetId', 'start', 'end'))
                if not same_audio:
                    raise
                warning = '이름/정보는 저장했습니다. 원본 오디오가 없어 재생하려면 파일을 다시 업로드해야 합니다.'
            else:
                if song['start'] >= meta['duration'] or song['end'] > meta['duration'] + .25:
                    raise ValueError(f'재생 구간이 파일 길이({meta["duration"]:.1f}초)를 초과합니다.')
        await self.store.save_song(team, song, old_name=old_name)
        if old_name and old_name != song['name'] and song['category'] == 'entrance':
            # Refresh existing Discord lineup messages; a display failure must not
            # roll back or misreport a successfully committed nickname change.
            for candidate in self.all_bots():
                if not hasattr(candidate, 'refresh_lineup'):
                    continue
                for guild in candidate.guilds:
                    if await self.store.get_team(guild.id, candidate.slot) == team:
                        try:
                            await candidate.refresh_lineup(guild)
                        except Exception:
                            log.warning('닉네임 변경 후 Discord 타순 표시 갱신 실패: bot=%s guild=%s',
                                        self.bot_label(candidate), guild.id)
        return response({'ok': True, 'name': song['name'], 'category': song['category'], 'warning': warning})

    async def delete_song(self, request):
        await self.store.delete_song(clean_name(request.query.get('team')), clean_name(request.query.get('name')),
                                     song_category(request.query.get('category', 'entrance')))
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
        resp.headers['Content-Disposition'] = (
            "attachment; filename*=UTF-8''" + quote(meta['name'], safe='')
            if request.query.get('download') == '1' else 'inline')
        return resp

    async def event(self, request):
        data = await body(request)
        team, key = clean_name(data.get('team')), str(data.get('key', ''))
        current = await self.store.event(team, key)
        custom_event = bool(current and current.get('isCustom') and key not in EVENTS)
        if key not in EVENTS and not custom_event:
            raise ValueError('알 수 없는 경기 상황입니다.')

        if data.get('restore') is True:
            if not current or not current.get('deleted'):
                raise ValueError('삭제된 상황이 아닙니다.')
            restored = {k: v for k, v in current.items() if k not in {'deleted', 'id'}}
            await self.store.set_event(team, key, restored or None)
            return response({'ok': True})
        if current and current.get('deleted'):
            raise ValueError('삭제된 경기 상황입니다. 먼저 복원하세요.')

        percent = (volume_percent(data['volumePercent']) if 'volumePercent' in data
                   else saved_volume_percent(current))
        base = {}
        if custom_event:
            base = {'label': clean_name(current.get('label') or key, '상황 이름'), 'isCustom': True}
            if 'label' in data:
                label = clean_name(data.get('label'), '상황 이름')
                existing = await self.store.events(team)
                for row in existing:
                    if row.get('id') != key:
                        row_label = str(row.get('label') or EVENTS.get(row.get('id'), ''))
                        if row_label.casefold() == label.casefold():
                            raise ValueError('같은 이름의 경기 상황이 이미 있습니다.')
                if any(k != key and v.casefold() == label.casefold() for k, v in EVENTS.items()):
                    raise ValueError('같은 이름의 기본 경기 상황이 이미 있습니다.')
                base['label'] = label

        previous_source = ({k: v for k, v in (current or {}).items() if k not in {'id', 'label', 'isCustom'}})

        # New multi-track API. Saving the list also migrates old songName/assetId records.
        if 'tracks' in data or 'playMode' in data:
            mode = str(data.get('playMode') or previous_source.get('playMode') or 'single')
            if mode not in {'single', 'random', 'sequence'}:
                raise ValueError('재생 방식은 단곡, 랜덤, 여러곡 순차 중에서 선택하세요.')
            tracks = (await self._validated_event_tracks(team, data['tracks'], current)
                      if 'tracks' in data else self._event_tracks(current))
            if not tracks and not custom_event:
                # Built-in situation with an empty list means restore bundled default sounds.
                await self.store.set_event(team, key, {'volumePercent': percent} if percent != 100 else None)
            else:
                await self.store.set_event(team, key, {**base, 'playMode': mode, 'tracks': tracks, 'volumePercent': percent})
            return response({'ok': True, 'playMode': mode, 'trackCount': len(tracks)})

        # Gain-only request for bundled/legacy event audio. No track list is lost.
        if 'volumePercent' in data and not any(k in data for k in ('assetId', 'songName')):
            await self.store.set_event(team, key, {**base, **previous_source, 'volumePercent': percent})
            return response({'ok': True})

        # Backward compatibility with the previous website/API.
        asset = data.get('assetId')
        if data.get('songName'):
            name = clean_name(data['songName'])
            category = song_category(data.get('category', 'situation'))
            if not await self.store.song(team, name, category):
                raise ValueError('선택한 라이브러리에 먼저 곡을 등록하세요.')
            await self.store.set_event(team, key, {**base, 'playMode': 'single',
                                                    'tracks': [{'type': 'song', 'songName': name, 'category': category}],
                                                    'songName': name, 'category': category})
        elif asset:
            meta = self.assets.get(str(asset))
            if 'volumePercent' not in data:
                matching = next((t for t in self._event_tracks(current) if t.get('assetId') == meta['id']), None)
                percent = saved_volume_percent(matching)
            await self.store.set_event(team, key, {**base, 'playMode': 'single',
                                                    'tracks': [{'type': 'asset', 'assetId': meta['id'], 'filename': meta['name'], 'volumePercent': percent}],
                                                    'assetId': meta['id'], 'filename': meta['name'], 'volumePercent': percent})
        elif custom_event and 'label' in data and 'assetId' not in data and 'songName' not in data:
            # Rename only: preserve every connected track and playback mode.
            await self.store.set_event(team, key, {**base, **previous_source})
        elif custom_event:
            await self.store.set_event(team, key, base)
        else:
            await self.store.set_event(team, key, None)
        return response({'ok': True})

    async def control(self, request):
        d = await body(request)
        target = 'secondary' if d.get('botTarget') == 'secondary' else 'primary'
        target_bot = self.target_bot(target)
        guild = self.guild(d.get('guildId'), target)
        team = clean_name(d.get('team') or await self.store.get_team(guild.id, target))
        action = d.get('action')
        channel_id = d.get('channelId') if action in {'connect', 'play', 'order', 'next', 'event'} else None
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel_id and channel is None:
            raise ValueError('선택한 통화방이 없습니다.')
        if channel is not None and hasattr(target_bot, 'ensure_distinct_voice_channel'):
            target_bot.ensure_distinct_voice_channel(guild, channel)
        player = target_bot.player
        if action == 'connect':
            await player.voice.connect(guild, channel)
        elif action == 'disconnect':
            await player.leave(guild)
        elif action == 'stop':
            player.stop(guild)
        elif action == 'volume':
            await player.volume(guild, team, int(d.get('value', -1)))
        elif action == 'play':
            song = await self.store.song(team, clean_name(d.get('name')), song_category(d.get('category', 'entrance')))
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
            current = await self.store.event(team, key)
            if key not in EVENTS and not (current and current.get('isCustom')):
                raise ValueError('알 수 없는 경기 상황입니다.')
            if current and current.get('deleted'):
                raise ValueError('삭제된 경기 상황입니다. 복원 후 재생하세요.')
            await player.event(guild, team, key, channel)
        else:
            raise ValueError('알 수 없는 명령입니다.')
        manage_idle = getattr(target_bot, 'manage_idle', None)
        if manage_idle:
            await manage_idle(guild)
        if action in {'order', 'next'} and hasattr(target_bot, 'refresh_lineup'):
            await target_bot.refresh_lineup(guild)
        return response({'ok': True, 'botTarget': target, 'botLabel': self.bot_label(target_bot),
                         'voice': player.voice.status(guild), 'nowPlaying': player.now.get(guild.id)})

    async def diagnostics(self, request):
        return response({
            'discordReady': self.bot.is_ready(), 'webOnly': self.s.web_only,
            'bots': self.bot_targets(),
            'storage': self.store.mode, 'fileStorage': storage_status(self.s),
            'apiVersion': 2, 'authMode': request['session'].get('mode', 'cookie'),
            'publicUrl': self.s.public_url, 'webUrl': self.s.web_url,
            'webOrigins': list(self.s.web_origins),
            'packages': {p: version(p) for p in ['discord.py', 'PyNaCl', 'davey', 'yt-dlp', 'yt-dlp-ejs', 'aiohttp']},
            'ffmpeg': bool(shutil.which(self.s.ffmpeg)), 'ffprobe': bool(shutil.which(self.s.ffprobe)),
            'deno': bool(shutil.which('deno')),
            'cookiesConfigured': bool(self.s.cookies_path or self.s.cookies_b64),
            'secureCookie': self.s.secure_cookie, 'maxUploadMb': self.s.max_upload_mb,
            'maxStorageMb': self.s.max_storage_mb,
            'checks': [
                '웹과 API는 같은 Railway 서비스에서 실행됨',
                '데이터베이스는 DATA_DIR/songs.sqlite3이며 Railway Volume 안에 저장',
                '웹 로그인 세션은 서버 재시작 또는 8시간 후 만료됨: 다시 로그인',
                'Discord Developer Portal → Bot → Message Content Intent 켜기 (!명령어용)',
                '통화방 권한 덮어쓰기에서 채널 보기·연결·말하기 허용',
                '서버 음소거 해제, 통화방 인원 제한 확인',
                '스테이지 자동 발언자 전환: 봇에 멤버 음소거(Mute Members) 권한 필요. 권한이 없으면 관리자가 발언자로 초대/전환 후 다시 재생',
                '같은 토큰을 두 봇에 중복 사용하지 않기. 보조 봇은 DISCORD_TOKEN_SECONDARY에 별도 토큰 사용',
                'Railway replica는 1개 유지. 한 프로세스 안에서 청팀/백팀 봇 두 계정을 함께 실행',
                '음성 연결은 UDP 송수신이 필요함. HTTP 포트만 열어서는 해결되지 않음',
                'YouTube 링크 재생에는 Deno와 yt-dlp-ejs도 필요함. Docker 이미지에 포함',
                'YouTube 차단은 음성 연결 오류와 별개. MP3 업로드로 구분 테스트',
                storage_status(self.s)['message'],
                '기존에 유실된 오디오는 자동 복원되지 않음: 라이브러리 수정에서 원본 재업로드',
                '자동 통화방 입장은 등장곡만 재생. 응원가·상황별 노래는 따로 재생',
            ],
        })

    async def export(self, request):
        resp = response(await self.store.export())
        resp.headers['Content-Disposition'] = 'attachment; filename="song-metadata-backup.json"'
        return resp

    async def backup(self, request):
        if self.backup_lock.locked():
            raise web.HTTPTooManyRequests(text='백업을 생성 중입니다. 잠시 뒤 다시 시도하세요.')
        async with self.backup_lock, self.assets.lock:
            metadata = await self.store.export()
            handle = tempfile.TemporaryFile(mode='w+b')
            task = asyncio.create_task(asyncio.to_thread(write_music_backup, handle, self.assets, metadata))
            try:
                await asyncio.shield(task)
                # aiohttp streams an IOBase payload and closes it on completion.
                # TemporaryFile is unlinked automatically, and no password DB is copied.
                resp = web.Response(body=handle, content_type='application/zip')
                resp.headers['Content-Disposition'] = 'attachment; filename="appearance-music-backup.zip"'
                return resp
            except BaseException:
                await asyncio.gather(task, return_exceptions=True)
                handle.close()
                raise

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.s.host, self.s.port).start()
        log.info('웹 관리 화면 시작 (port=%s). 비밀번호와 토큰은 공유하지 마세요.', self.s.port)

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
