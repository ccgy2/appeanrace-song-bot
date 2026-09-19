"""Configure Firebase Hosting + Railway locally. Does NOT publish or change cloud accounts.

Usage: python setup_deploy.py
Or: python setup_deploy.py --project-id my-project --firebase-url https://my-site.web.app --railway-url https://my-bot.up.railway.app
Custom Firebase domains additionally require --site-id (the Hosting site ID).
Only the Python standard library is needed; no bot credentials are requested.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def origin(value: str) -> str:
    value = value.strip().rstrip('/')
    u = urlsplit(value)
    if (u.scheme != 'https' or not u.hostname or u.username is not None or u.password is not None
            or u.path or u.query or u.fragment or any(c.isspace() for c in value)
            or '*' in value or '\\' in value):
        raise ValueError('인터넷 주소는 경로 없는 HTTPS 주소여야 합니다. 예: https://my-bot.up.railway.app')
    host = u.hostname.encode('idna').decode('ascii').lower()
    if host in {'localhost', '127.0.0.1', '::1'} or host.endswith('.railway.internal'):
        raise ValueError('로컬/내부 주소가 아니라 실제 공개 HTTPS 도메인을 사용하세요.')
    port = u.port
    return f'https://{host}' + (f':{port}' if port and port != 443 else '')


def site_from_url(url: str) -> str:
    host = urlsplit(url).hostname or ''
    for suffix in ('.web.app', '.firebaseapp.com'):
        if host.endswith(suffix):
            site = host[:-len(suffix)]
            if re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', site) and '--' not in site:
                return site
    return ''


def configure(root: Path, project_id: str, firebase_url: str, railway_url: str, site_id: str = '') -> dict:
    project_id = project_id.strip()
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', project_id):
        raise ValueError('Firebase 프로젝트 ID를 확인하세요. 프로젝트 표시 이름이 아니라 영문/숫자/하이픈으로 된 ID입니다.')
    frontend, backend = origin(firebase_url), origin(railway_url)
    if frontend == backend:
        raise ValueError('Firebase 웹 주소와 Railway 봇 API 주소는 서로 달라야 합니다.')
    detected = site_from_url(frontend)
    site = site_id.strip() or detected
    if not site or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', site) or '--' in site:
        raise ValueError('커스텀 도메인은 Firebase Hosting의 사이트 ID를 --site-id로 지정하세요. 미리보기 채널 주소 대신 실제 사이트 주소를 쓰세요.')
    if detected and site != detected:
        raise ValueError('Firebase 주소의 사이트 ID와 --site-id가 다릅니다. 잘못된 사이트에 배포하지 않도록 설정을 확인하세요.')
    manifest = json.loads((root / 'firebase.json').read_text(encoding='utf-8'))
    hosting = manifest.get('hosting')
    if not isinstance(hosting, dict) or hosting.get('public') != 'static':
        raise ValueError('이 설정 도구는 포함된 단일 사이트용 firebase.json (public=static)에서 실행하세요.')
    hosting['site'] = site
    hosting.pop('target', None)
    csp = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           f"connect-src 'self' {backend}; media-src 'self' blob:; object-src 'none'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    for rule in hosting['headers']:
        for header in rule['headers']:
            if header['key'].lower() == 'content-security-policy':
                header['value'] = csp
    allowed = list(dict.fromkeys([frontend, f'https://{site}.web.app', f'https://{site}.firebaseapp.com']))
    variables = {'PUBLIC_URL': backend, 'WEB_URL': frontend, 'WEB_ORIGINS': ','.join(allowed),
                 'WEB_ENABLED': 'true', 'WEB_ONLY': 'false', 'HOST': '0.0.0.0', 'PORT': '8080'}
    values = '\n'.join(f'{k}={v}' for k, v in variables.items()) + '\n'
    payloads = {
        'deployment-link.json': json.dumps({'api_url': backend, 'web_url': frontend, 'web_origins': allowed}, indent=2) + '\n',
        'static/config.js': '// Public URL only. Never store passwords or tokens in this file.\nwindow.APPEARANCE_CONFIG = Object.freeze(' + json.dumps({'API_BASE_URL': backend}, indent=2) + ');\n',
        'firebase.json': json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        '.firebaserc': json.dumps({'projects': {'default': project_id}}, indent=2) + '\n',
        'railway-web-variables.txt': values,
    }
    # Keep the separately deployed website in sync when this advanced helper is used.
    if (root / '웹사이트' / 'firebase.json').is_file():
        payloads['웹사이트/static/config.js'] = payloads['static/config.js']
        payloads['웹사이트/firebase.json'] = payloads['firebase.json']
        payloads['웹사이트/.firebaserc'] = payloads['.firebaserc']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for name, text in payloads.items():
        target = root / name
        if target.exists():
            backup = root / '.deploy-backups' / stamp / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return {'project': project_id, 'site': site, 'frontend': frontend, 'backend': backend, 'variables': variables}


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-id')
    parser.add_argument('--firebase-url')
    parser.add_argument('--railway-url')
    parser.add_argument('--site-id', default='')
    args = parser.parse_args()
    print('Firebase 웹사이트 ↔ Railway 봇 연결 설정 (클라우드 배포는 실행하지 않습니다.)')
    try:
        project = args.project_id or input('Firebase 프로젝트 ID: ').strip()
        frontend = args.firebase_url or input('Firebase 웹 주소 (https://...web.app): ').strip()
        backend = args.railway_url or input('Railway 공개 주소 (https://...up.railway.app): ').strip()
        site = args.site_id
        if not site and not site_from_url(frontend):
            site = input('이 커스텀 도메인의 Firebase Hosting 사이트 ID: ').strip()
        result = configure(ROOT, project, frontend, backend, site)
    except (ValueError, OSError, EOFError, KeyboardInterrupt) as exc:
        print(f'설정을 완료하지 못했습니다: {exc}', file=sys.stderr)
        return 1
    print(f"\n설정 완료 — 프로젝트: {result['project']} / Hosting 사이트: {result['site']}")
    print('1. railway-web-variables.txt의 7개 항목을 Railway Variables에 추가/수정하세요.')
    print('   기존 DISCORD_TOKEN, FIREBASE_SERVICE_ACCOUNT, OWNER_ID, WEB_ADMIN_PASSWORD는 지우지 마세요.')
    print('2. 이번 수정본 전체를 Railway에 재배포하세요. 공개 도메인의 대상 포트는 8080입니다.')
    print('3. 이 폴더에서 firebase login, firebase deploy --only hosting 을 실행하세요.')
    print(f"4. {result['frontend']} 에서 연결 상태를 확인하고 관리자 비밀번호로 로그인하세요.")
    print('영구 음원 저장을 위한 Railway 볼륨/DATA_DIR은 기존 값을 확인하세요. 이 도구는 변경하지 않습니다.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
