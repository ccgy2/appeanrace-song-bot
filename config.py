"""환경변수 설정. 비밀값은 웹 응답에 포함하지 않는다."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')


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

    @classmethod
    def from_env(cls) -> 'Settings':
        url = os.getenv('PUBLIC_URL', '').strip().rstrip('/')
        if url:
            parsed = urlsplit(url)
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment):
                raise ValueError('PUBLIC_URL에는 경로 없이 사이트 주소만 입력하세요. 예: https://example.com')
            try:
                parsed.port
            except ValueError:
                raise ValueError('PUBLIC_URL의 포트 번호가 올바르지 않습니다.') from None
        data_dir = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).expanduser().resolve()
        s = cls(
            token=os.getenv('DISCORD_TOKEN', '').strip(),
            firebase_key=os.getenv('FIREBASE_SERVICE_ACCOUNT', '').strip(),
            owner_id=int(os.getenv('OWNER_ID', '0') or 0),
            data_dir=data_dir,
            host=os.getenv('HOST', '0.0.0.0'),
            port=int(os.getenv('PORT', '8080')),
            web_password=os.getenv('WEB_ADMIN_PASSWORD', ''),
            web_enabled=flag('WEB_ENABLED', True),
            public_url=url,
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
        if s.web_only and not s.web_password:
            raise ValueError('웹 관리 화면을 쓰려면 WEB_ADMIN_PASSWORD를 설정하세요.')
        if url and not url.startswith(('https://', 'http://')):
            raise ValueError('PUBLIC_URL은 http:// 또는 https://로 시작해야 합니다.')
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s
