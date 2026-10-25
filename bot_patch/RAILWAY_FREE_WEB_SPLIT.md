> 2026-10-25: 이 폴더는 선택적 이전 배포 패치 사본입니다. 이번 절약 버전은 루트의 전체 소스와 `youtube_resolver.py`, `youtube_worker.py`까지 함께 반영해야 합니다. 두 파일만 복사해 전체 절약 업데이트가 완료되는 것은 아닙니다. 2차 버전은 `audio_runtime.py`, `resource_usage.py`, `release.py`도 필요합니다. 먼저 `RAILWAY_COST_REDUCTION_V2.md`를 확인하세요.

# Railway 봇/API + GitHub Pages 무료 웹 분리

1. BOT 패치의 `config.py`, `webapp.py`를 기존 저장소 루트에 덮어쓰고 GitHub에 커밋합니다.
2. Railway Variables에 `SERVE_WEB_STATIC=false`를 추가합니다.
3. WEBSITE ZIP의 내용은 **별도의 Public GitHub 저장소** 루트에 업로드합니다. 토큰/.env/DB/음원은 올리지 않습니다.
4. 그 저장소 Settings → Pages → Build and deployment → Deploy from a branch → main / (root) → Save.
5. 발급된 주소가 `https://사용자명.github.io/저장소명/`이면 Railway Variables에 `GITHUB_PAGES_ORIGIN=https://사용자명.github.io` 를 추가합니다. (경로 없이 origin만)
6. Railway를 최신 커밋으로 재배포합니다. Pages 주소에서 로그인/재생을 확인합니다.

Railway에는 Discord 봇과 웹 제어에 꼭 필요한 API가 계속 실행됩니다. 따라서 Railway 사용량 자체가 0이 되는 패치가 아닙니다. 정적 HTML/CSS/JS/PWA 전송을 Railway에서 제거하는 패치입니다.
