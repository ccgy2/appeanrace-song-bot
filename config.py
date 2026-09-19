"""환경변수 설정. 비밀값은 웹 응답에 포함하지 않는다."""
from __future__ import annotations
import os
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')


def normalize_origin(value: str, label: str = '웹 주소') -> str:
    """Exact origins only. Public deployments require HTTPS; HTTP is local-only."""
    value = value.strip().rstrip('/')
    if not value:
        return ''
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        raise ValueError(f'{label}의 주소/포트가 올바르지 않습니다.') from None
    if (u.scheme not in {'http', 'https'} or not u.hostname or u.username is not None
            or u.password is not None or u.path or u.query or u.fragment
            or any(c.isspace() for c in value) or '*' in value or '\\' in value):
        raise ValueError(f'{label}에는 경로 없이 정확한 HTTPS 주소만 입력하세요.')
    if u.scheme == 'http' and u.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError(f'{label}: 인터넷 공개 주소는 https://를 사용하세요.')
    host = u.hostname.lower().encode('idna').decode('ascii')
    if ':' in host:
        host = f'[{host}]'
    suffix = f':{port}' if port and port != (443 if u.scheme == 'https' else 80) else ''
    return f'{u.scheme}://{host}{suffix}'



def load_deployment_link() -> dict:
    """This optional file contains public deployment addresses, never credentials.

    When present it is the source of truth for these three URL settings. This
    avoids old PUBLIC_URL / WEB_ORIGIN variables breaking the linked release.
    Removing the file restores the generic environment-variable configuration.
    Malformed files fail closed; they must not broaden the origin allow-list.
    """
    path = ROOT / 'deployment-link.json'
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        api = normalize_origin(raw['api_url'], 'deployment-link.json api_url')
        front = normalize_origin(raw['web_url'], 'deployment-link.json web_url')
        extra = raw.get('web_origins', [])
        if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
            raise ValueError('web_origins must be a list of exact origins')
        allowed = tuple(dict.fromkeys([front, *(normalize_origin(x) for x in extra)]))
        if (not api.startswith('https://') or not front.startswith('https://')
                or api == front or any(not x.startswith('https://') for x in allowed)):
            raise ValueError('linked deployment requires distinct public HTTPS addresses')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ValueError('deployment-link.json의 공개 주소 설정을 확인하세요.') from exc
    return {'api_url': api, 'web_url': front, 'web_origins': allowed}


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'true', '1', 'yes', 'on'}


@dataclass(frozen=True)
class Settings:
    token: str = ''
    firebase_key: str = ''
    owner_id: int = 0
    data_dir: Path = ROOT / 'data'
    host: str = '0.0.0.0'
    port: int = 8080
    web_password: str = ''
    web_enabled: bool = True
    public_url: str = ''
    web_url: str = ''
    web_origins: tuple[str, ...] = ()
    secure_cookie: bool = False
    web_only: bool = False
    idle_seconds: int = 300
    max_upload_mb: int = 25
    max_storage_mb: int = 1024
    ffmpeg: str = 'ffmpeg'
    ffprobe: str = 'ffprobe'
    cookies_path: str = ''
    cookies_b64: str = ''
    enable_members_intent: bool = False
    on_railway: bool = False
    volume_path: Path | None = None

    @classmethod
    def from_env(cls) -> 'Settings':
        linked = load_deployment_link()
        if linked:
            # Deliberately ignore legacy URL-only variables in this prelinked build.
            # Discord/Firebase secrets, password, storage, port and bot mode remain
            # controlled by the existing Railway environment variables.
            url, web_url = linked['api_url'], linked['web_url']
            origins = list(linked['web_origins'])
            logging.getLogger(__name__).info(
                '연결 설정 적용 (deployment-link.json): 웹=%s / API=%s', web_url, url)
        else:
            url = normalize_origin(os.getenv('PUBLIC_URL', ''), 'PUBLIC_URL (Railway)')
            web_url = normalize_origin(os.getenv('WEB_URL', ''), 'WEB_URL (Firebase)')
            origins = []
            raw = ','.join([os.getenv('WEB_ORIGINS', ''), os.getenv('WEB_ORIGIN', ''), web_url])
            for item in raw.split(','):
                if item.strip():
                    origin = normalize_origin(item, 'WEB_ORIGINS')
                    if origin not in origins:
                        origins.append(origin)
        volume_raw = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '').strip()
        volume_path = Path(volume_raw).expanduser().resolve() if volume_raw else None
        # Keep explicit DATA_DIR (no silent data migration). Otherwise use the
        # Railway-provided volume path before the container/local default.
        fallback = volume_raw or os.getenv('APP_DOCKER_DATA_DIR', '').strip() or str(ROOT / 'data')
        data_dir = Path(os.getenv('DATA_DIR', '').strip() or fallback).expanduser().resolve()
        on_railway = bool(volume_raw or any(os.getenv(k) for k in (
            'RAILWAY_ENVIRONMENT_ID', 'RAILWAY_PROJECT_ID', 'RAILWAY_SERVICE_ID', 'RAILWAY_PUBLIC_DOMAIN')))

        s = cls(
            token=os.getenv('DISCORD_TOKEN', '').strip(),
            firebase_key=os.getenv('FIREBASE_SERVICE_ACCOUNT', '').strip(),
            owner_id=int(os.getenv('OWNER_ID', '0') or 0),
            data_dir=data_dir,
            on_railway=on_railway,
            volume_path=volume_path,
            host=os.getenv('HOST', '0.0.0.0'),
            port=int(os.getenv('PORT', '8080')),
            web_password=os.getenv('WEB_ADMIN_PASSWORD', ''),
            web_enabled=flag('WEB_ENABLED', True),
            public_url=url,
            web_url=web_url,
            web_origins=tuple(origins),
            secure_cookie=flag('WEB_COOKIE_SECURE', url.startswith('https://')),
            web_only=flag('WEB_ONLY'),
            idle_seconds=max(10, int(os.getenv('VOICE_IDLE_SECONDS', '300'))),
            max_upload_mb=max(1, int(os.getenv('MAX_UPLOAD_MB', '25'))),
            max_storage_mb=max(1, int(os.getenv('MAX_STORAGE_MB', '1024'))),
            ffmpeg=os.getenv('FFMPEG_PATH', 'ffmpeg'),
            ffprobe=os.getenv('FFPROBE_PATH', 'ffprobe'),
            cookies_path=os.getenv('YTDLP_COOKIES_PATH', '').strip(),
            cookies_b64=os.getenv('YTDLP_COOKIES_B64', '').strip(),
            enable_members_intent=flag('ENABLE_MEMBERS_INTENT'),
        )
        if not 1 <= s.port <= 65535:
            raise ValueError('PORT는 1~65535여야 합니다.')
        if s.web_password and (len(s.web_password) < 12 or s.web_password.startswith('CHANGE_ME')):
            raise ValueError('WEB_ADMIN_PASSWORD를 12자 이상의 새 비밀번호로 바꾸세요.')
        if not s.web_only and not s.token:
            raise ValueError('DISCORD_TOKEN을 설정하세요. 화면만 확인하려면 WEB_ONLY=true를 사용하세요.')
        if s.web_enabled and not s.web_password:
            raise ValueError('웹 관리 화면을 쓰려면 WEB_ADMIN_PASSWORD를 설정하세요. Railway Variables에 12자 이상의 관리자 비밀번호를 지정하세요.')
        if url and not url.startswith(('https://', 'http://')):
            raise ValueError('PUBLIC_URL은 http:// 또는 https://로 시작해야 합니다.')
        s.data_dir.mkdir(parents=True, exist_ok=True)
        from persistence import storage_status
        report = storage_status(s)
        logger = logging.getLogger(__name__)
        (logger.warning if report['status'] == 'unsafe' else logger.info)(
            '음원 저장소: %s | DATA_DIR=%s | %s', report['label'], s.data_dir, report['message'])
        return s
