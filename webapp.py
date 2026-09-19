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
from validation import EVENTS, clean_name, validate_song, song_category, SONG_CATEGORIES
from persistence import storage_status
from backups import write_music_backup

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
        self.app = web.Application(middlewares=[self.cors, self.security], client_max_size=(self.s.max_upload_mb + 1) * 1024 * 1024)
        self.app.add_routes([
            web.get('/', self.index),
            web.get('/assets/{name}', self.static),
            web.get('/{name:app\\.js|config\\.js|style\\.css}', self.static),
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
                if role not in {'admin', 'player'}:
                    raise web.HTTPForbidden(text='관리자 승인 대기 중인 계정입니다.')
                # 재생자는 등장곡 파일 업로드와 곡 등록/수정까지 가능하다.
                # 팀/타순/효과음/백업/회원 관리와 곡 삭제는 관리자 전용이다.
                admin_only = (
                    request.path.startswith('/api/users')
                    or request.path in {'/api/team', '/api/lineup', '/api/events', '/api/export', '/api/backup'}
                    or (request.path == '/api/songs' and request.method == 'DELETE')
                )
                if admin_only and role != 'admin':
                    raise web.HTTPForbidden(text='관리자 권한이 필요합니다.')
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
        if name not in {'app.js', 'style.css', 'config.js'}:
            raise web.HTTPNotFound()
        return web.FileResponse(ROOT / 'static' / name)

    async def health(self, request):
        return response({'web': 'ok', 'discord': 'ready' if self.bot.is_ready() else 'offline'})

    async def connection(self, request):
        # Safe, unauthenticated endpoint: checks API identity + CORS, not just a 200 page.
        return response({'service': 'appearance-song-bot', 'apiVersion': 2,
                         'authMode': 'bearer', 'web': 'ok', 'build': 'library-20260919-1',
                         'capabilities': ['library-categories', 'atomic-rename', 'storage-check'],
                         'discordReady': self.bot.is_ready(), 'webOnly': self.s.web_only})

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
        return response({'ok':True,'message':'가입 신청 완료! 관리자가 재생자로 승인하면 로그인할 수 있습니다.'},201)

    async def login(self, request):
        await self._ensure_admin(); now,window=self._limit_login(request); d=await body(request)
        mode=d.get('authMode','cookie'); username=str(d.get('username') or 'admin').strip().lower(); supplied=str(d.get('password',''))
        if mode not in {'cookie','bearer'}: raise ValueError('지원하지 않는 로그인 방식입니다.')
        user=await self.store.web_user(username)
        if not user or not await asyncio.to_thread(password_ok,supplied,user.get('passwordHash','')): raise web.HTTPUnauthorized(text='아이디 또는 비밀번호가 맞지 않습니다.')
        if not user.get('enabled',True): raise web.HTTPForbidden(text='사용이 중지된 계정입니다.')
        if user.get('role')=='pending': raise web.HTTPForbidden(text='관리자 승인 대기 중입니다.')
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
        if role not in {'pending','player'}: raise ValueError('승인 대기 또는 재생자만 지정할 수 있습니다.')
        await self.store.save_web_user(username,{'role':role,'enabled':bool(d.get('enabled',user.get('enabled',True)))},True)
        self.sessions = {k:v for k,v in self.sessions.items() if v.get('username') != username}
        return response({'ok':True})

    async def remove_user(self, request):
        username=request.match_info['username'].strip().lower()
        if username=='admin': raise ValueError('기본 관리자 계정은 삭제할 수 없습니다.')
        await self.store.delete_web_user(username); self.sessions={k:v for k,v in self.sessions.items() if v.get('username')!=username}; return response({'ok':True})

    async def session(self, request):
        return response({'csrf':request['session']['csrf'],'user':{k:request['session'].get(k) for k in ('username','displayName','role')}})

    async def logout(self, request):
        self.sessions.pop(request['session_key'], None)
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
        songs = library['entrance']
        channels = []
        if guild:
            for c in guild.voice_channels:
                perms = c.permissions_for(guild.me) if guild.me else None
                channels.append({'id': str(c.id), 'name': c.name, 'members': len([m for m in c.members if not m.bot]), 'available': bool(perms and perms.view_channel and perms.connect and perms.speak)})
        return response({
            'ready': self.bot.is_ready(), 'webOnly': self.s.web_only, 'storage': self.store.mode,
            'guilds': guilds, 'guildId': str(guild.id) if guild else None,
            'teams': teams, 'team': team, 'activeTeam': active, 'songs': songs,
            'library': library, 'categories': SONG_CATEGORIES, 'fileStorage': storage_status(self.s),
            'lineup': await self.store.lineup(team), 'state': state, 'channels': channels,
            'voice': self.bot.player.voice.status(guild) if guild else None,
            'nowPlaying': self.bot.player.now.get(guild.id) if guild else None,
            'events': self._event_rows(events),
            'maxUploadMb': self.s.max_upload_mb,
        })

    def _event_rows(self, events: dict[str, dict]) -> list[dict]:
        rows = []
        for key, label in EVENTS.items():
            doc = events.get(key)
            source = None
            if doc:
                source = {k: v for k, v in doc.items() if k not in {'id', 'label', 'isCustom'}} or None
            rows.append({'key': key, 'label': label, 'custom': source,
                         'customEvent': False, 'bundledCount': len(self.assets.bundled(key))})
        extras = []
        for key, doc in events.items():
            if key in EVENTS or not doc.get('isCustom'):
                continue
            label = str(doc.get('label') or key)
            source = {k: v for k, v in doc.items() if k not in {'id', 'label', 'isCustom'}} or None
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
        await self.store.set_event(team, key, {'label': label, 'isCustom': True})
        return response({'ok': True, 'key': key, 'label': label}, 201)

    async def delete_event(self, request):
        team = clean_name(request.query.get('team'), '팀 이름')
        key = str(request.query.get('key', ''))
        current = await self.store.event(team, key)
        if not current or not current.get('isCustom') or key in EVENTS:
            raise ValueError('직접 추가한 경기 상황만 삭제할 수 있습니다.')
        await self.store.set_event(team, key, None)
        return response({'ok': True})

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
            if hasattr(self.bot, 'refresh_lineup'):
                for guild in self.bot.guilds:
                    if await self.store.get_team(guild.id) == team:
                        try:
                            await self.bot.refresh_lineup(guild)
                        except Exception:
                            log.warning('닉네임 변경 후 Discord 타순 표시 갱신 실패: guild=%s', guild.id)
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

        asset = data.get('assetId')
        previous_source = ({k: v for k, v in current.items() if k not in {'id', 'label', 'isCustom'}} if custom_event else {})
        if data.get('songName'):
            name = clean_name(data['songName'])
            if not await self.store.song(team, name, 'situation'):
                raise ValueError('상황별 노래 라이브러리에 먼저 곡을 등록하세요.')
            await self.store.set_event(team, key, {**base, 'songName': name, 'category': 'situation'})
        elif asset:
            meta = self.assets.get(str(asset))
            await self.store.set_event(team, key, {**base, 'assetId': meta['id'], 'filename': meta['name']})
        elif custom_event and 'label' in data and 'assetId' not in data and 'songName' not in data:
            # 이름만 바꿀 때는 기존 사운드 연결을 그대로 유지한다.
            await self.store.set_event(team, key, {**base, **previous_source})
        elif custom_event:
            # 직접 만든 상황은 연결만 해제하고 상황 이름 자체는 남긴다.
            await self.store.set_event(team, key, base)
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
            'storage': self.store.mode, 'fileStorage': storage_status(self.s),
            'apiVersion': 2, 'authMode': request['session'].get('mode', 'cookie'),
            'publicUrl': self.s.public_url, 'webUrl': self.s.web_url,
            'webOrigins': list(self.s.web_origins),
            'packages': {p: version(p) for p in ['discord.py', 'PyNaCl', 'davey', 'yt-dlp', 'yt-dlp-ejs', 'aiohttp', 'firebase-admin']},
            'ffmpeg': bool(shutil.which(self.s.ffmpeg)), 'ffprobe': bool(shutil.which(self.s.ffprobe)),
            'deno': bool(shutil.which('deno')),
            'cookiesConfigured': bool(self.s.cookies_path or self.s.cookies_b64),
            'secureCookie': self.s.secure_cookie, 'maxUploadMb': self.s.max_upload_mb,
            'maxStorageMb': self.s.max_storage_mb,
            'checks': [
                'Firebase → Railway: static/config.js의 API_BASE_URL은 Railway 공개 HTTPS 주소',
                'Railway WEB_ORIGINS에 현재 Firebase 웹사이트 주소를 정확히 등록',
                '웹 로그인 세션은 서버 재시작 또는 8시간 후 만료됨: 다시 로그인',
                'Discord Developer Portal → Bot → Message Content Intent 켜기 (!명령어용)',
                '통화방 권한 덮어쓰기에서 채널 보기·연결·말하기 허용',
                '서버 음소거 해제, 통화방 인원 제한 확인',
                '같은 DISCORD_TOKEN으로 봇을 두 곳에서 실행하지 않기 (replica=1)',
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
