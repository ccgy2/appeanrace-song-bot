# 기존 Railway 봇 + Firebase 웹사이트 연결본

**연결 주소 설정이 끝난 소스입니다. 클라우드에 자동 배포된 상태는 아닙니다.**

- 관리 화면: https://appearance-song.web.app
- 기존 봇 API: https://appeanrace-song-bot-production.up.railway.app
- 빌드 표시: `linked-20260919-1`

`appeanrace`로 시작하는 Railway 주소는 사용자가 보낸 철자를 그대로 사용했습니다.
새 봇이나 새 Railway 서비스를 만들지 않습니다. 기존 GitHub 저장소와 Railway 서비스를 그대로 사용합니다.

## 1. 봇용 ZIP → 기존 GitHub 저장소

`appearance-railway-bot-linked.zip`을 풀고 기존 봇 저장소의 파일을 교체합니다.
압축 내부에는 추가 최상위 폴더 없이 `main.py`, `config.py`, `webapp.py`, `deployment-link.json`, `Dockerfile`, `railway.json`, `static/`, `sounds/` 등이 들어 있습니다.
이 파일들이 기존 저장소의 봇 실행 루트에 오도록 올리세요. ZIP 파일 자체를 GitHub에 올리는 것이 아닙니다.

**웹만 교체하면 안 됩니다.** 기존 봇이 이전 단일 사이트용 API라면 Firebase 웹의 새 로그인 요청을 처리하지 못합니다.
이번 봇용 파일을 같은 저장소에 반영한 뒤, 연결된 기존 Railway 서비스에 해당 변경을 배포하세요.
시작 명령은 포함된 `railway.json`의 `python main.py`입니다.

기존 `.env`, `DISCORD_TOKEN`, `FIREBASE_SERVICE_ACCOUNT`, `OWNER_ID`, `DATA_DIR`, 업로드 볼륨, YouTube 쿠키 설정을 삭제하거나 초기화하지 마세요.
실제 `.env`, 개인 키, 쿠키 파일을 GitHub에 올리지 마세요.
Firebase 키를 비우면 기존 Firestore 대신 새 로컬 SQLite 저장소를 사용하므로 기존 곡이 안 보일 수 있습니다.
`WEB_ENABLED=true`, `WEB_ONLY=false`가 실제 봇 + 웹 실행 설정입니다. 기존 웹이 켜져 있고 봇이 실행 중이면 유지하세요.

## 2. 웹사이트용 ZIP → 기존 Windows 폴더

`appearance-firebase-website-linked.zip`의 **내용물**을 아래 폴더에 덮어씁니다.

```text
G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트
```

최종 구조는 다음과 같습니다. ZIP 안에 또 `웹사이트` 폴더가 중첩되지 않도록 하세요.

```text
웹사이트\
├─ .firebaserc           ← 기존 프로젝트 연결 파일: 삭제하지 않음
├─ firebase.json         ← 이번 ZIP의 파일
├─ deploy-firebase.bat
├─ scripts\check-hosting.cjs
└─ static\
   ├─ index.html
   ├─ config.js          ← Railway 주소가 미리 들어 있음
   ├─ app.js
   └─ style.css
```

이 ZIP에는 `.firebaserc`를 넣지 않았습니다. Hosting 사이트 이름만으로 실제 소유 프로젝트 ID를 추측하지 않고, 이미 사용자가 배포하던 프로젝트 연결을 보존하기 위해서입니다.
기존 폴더를 통째로 삭제하지 말고 필요한 파일을 덮어쓰세요.

**명령 프롬프트(CMD)** 에서:

```bat
cd /d "G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트"
firebase deploy --only hosting
```

PowerShell을 쓰는 경우 첫 줄만 다음으로 바꿉니다.

```powershell
Set-Location -LiteralPath 'G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트'
```

기존 프로젝트를 정상적으로 선택한 상태라면 `firebase init`이나 `setup_deploy.py`를 다시 실행하지 않습니다.
`No currently active project` 오류가 나는 경우에만 `firebase use --add`로 **기존 appearance-song 사이트를 소유한 프로젝트**를 선택하세요. 새 프로젝트를 만들 필요가 없습니다.
배포 설정은 `site=appearance-song`, `public=static`입니다. 다른 사이트가 아닌 기존 봇 관리 사이트를 업데이트합니다.
`static`만 공개되며 봇 소스와 비밀번호는 배포 폴더에 들어가지 않습니다.

## 3. 로그인

브라우저에서 **https://appearance-song.web.app** 에 접속합니다.
웹에서 Railway 주소를 저장하거나 별도 주소 설정 도구를 실행하지 않습니다.

관리자 비밀번호는 기존 Railway 봇 서비스의 `Variables → WEB_ADMIN_PASSWORD`에 정한 값입니다.
기존 비밀번호를 알고 있으면 그대로 쓰세요. 별도로 새 값을 만들 필요가 없습니다.
기억하지 못하거나 아직 설정하지 않았다면 **그 항목 하나만** 본인이 정한 12자 이상의 새 비밀번호로 변경하고 Railway에 변경 사항을 배포합니다.
실제 비밀번호/봇 토큰을 대화에 보내거나 `static/config.js`에 넣지 마세요.

`웹 연결 정상 · 봇 온라인`은 API 연결과 Discord 로그인 상태를 표시합니다. 실제 음성 송출 성공을 보장하는 표시는 아닙니다.
로그인 후 서버 선택 → 팀 선택 → 통화방 선택/연결 → 곡 등록/재생 순서로 확인하세요.

## 이번 연결 설정의 동작

`deployment-link.json`은 **비밀정보가 없는 공개 주소 설정**입니다. GitHub에 올려도 됩니다.
이번 사용자 전용 묶음은 이 파일이 있을 때 그 안의 정확한 주소를 사용합니다.
이전 `PUBLIC_URL`, `WEB_URL`, `WEB_ORIGINS`, `WEB_ORIGIN` 환경변수 값이 잘못 남아 있어도 이 연결본의 주소가 우선합니다.
나머지 비밀번호, Discord/Firebase 키, 저장 위치, 포트 설정은 기존 환경변수를 유지합니다.

허용되는 외부 웹 주소는 `https://appearance-song.web.app` 및 같은 Hosting 사이트의 `https://appearance-song.firebaseapp.com`입니다.
Railway 주소 자체에서의 로그인도 허용합니다. 임의 사이트나 `*`를 허용하지 않습니다.
`WEB_URL` 안내를 쓰는 `!웹` 명령은 Firebase 관리 화면 주소를 반환합니다.

주소를 나중에 변경할 때는 봇의 `deployment-link.json`, 웹의 `static/config.js`, 웹의 `firebase.json` 안 `connect-src`를 함께 변경해야 합니다.
고급 설정 도구 `setup_deploy.py`도 연결 프로필을 갱신하도록 수정했습니다. 이미 두 주소가 정해진 이번 사용에는 실행할 필요가 없습니다.
환경변수만으로 설정하는 일반 모드로 되돌릴 때에만 `deployment-link.json`을 제거하고 정확한 URL 환경변수를 지정하세요.

## 접근 차단 범위

웹사이트의 정적 HTML/CSS/JS 자체는 공개 파일입니다. 로그인 없이 소스를 읽을 수 없는 비공개 파일 호스팅은 아닙니다.
반면 노래 목록, 타순, 효과음, 음원 업로드/열람, 백업, Discord 제어 API는 서버에서 인증합니다.
비로그인 요청은 `401`, 허용되지 않은 웹 출처는 `403`으로 차단합니다. 연결 확인용 `/healthz`, `/api/connection`만 공개이며 관리자 데이터나 비밀키를 포함하지 않습니다.
브라우저에서는 Bearer 토큰과 CSRF 토큰을 사용하므로 Firebase ↔ Railway 사이의 제3자 쿠키 허용에 의존하지 않습니다.
세션은 최대 8시간이며 로그아웃 또는 서버 재시작 시 무효화됩니다. 웹 토큰은 sessionStorage에 보관되므로 브라우저의 세션 복원 동작에 따라 탭 복원 시 남을 수 있습니다. 확실히 종료하려면 로그아웃하세요.
잘못된 로그인 시도는 서버가 관측하는 접속 주소 기준 10회/5분으로 제한됩니다. 프록시 뒤에서는 여러 사용자가 제한을 공유할 수 있습니다.

## 기존 데이터와 효과음

이번 수정은 연결 설정, 웹 로그인 화면의 주소 입력 제거, 배포 구성을 대상으로 합니다.
가장 최근 제공한 로그인 보호본과 비교하여 `main.py`, `player.py`, `voice.py`, `storage.py`, `assets.py`, `validation.py`, `requirements.txt`는 동일합니다.
처음 올린 원본 ZIP의 `sounds/` 내부 52개 파일은 SHA-256 비교 결과 모두 동일합니다. 상세 결과는 `tests/sounds-preservation.json`에 있습니다.
실제 기존 Firestore/업로드 볼륨에는 이 제작 과정에서 접근하거나 변경하지 않았습니다.

직접 올린 오디오는 `DATA_DIR/uploads`에 있으므로 기존 Railway 영구 볼륨과 경로를 유지하세요.
같은 봇 토큰은 한 인스턴스에서 실행합니다. 다른 서비스에 동일 봇을 새로 켜지 마세요.

## 자주 쓰는 봇 명령

`!웹`, `!입장`, `!퇴장`, `!정지`, `!진단`, `!도움`을 사용할 수 있습니다.
등록 예: `!저장 김선수 / https://youtu.be/영상ID / 0:10~0:40`
등장곡 관리와 재생의 상세 명령은 `!도움`에서 확인하세요.

## 검증 범위

이번 실행 환경에서 API/설정/저장소 테스트 74개를 통과했습니다.
실제 Discord 음성 접속, 실제 Firebase/Railway 배포, 실제 사용자 비밀번호 로그인, YouTube 재생을 확인한 것은 아닙니다.
브라우저 자동 탐색은 실행 환경 정책에 의해 차단됐습니다. 이번 버전의 브라우저 종단간 테스트가 통과했다고 주장하지 않습니다.
전체 검증 결과는 `TEST_REPORT.md`에 적었습니다.

## 참고 공식 문서

- Railway Variables: https://docs.railway.com/variables
- Firebase Hosting 배포: https://firebase.google.com/docs/hosting/quickstart
- Firebase 공개 폴더/보안 헤더: https://firebase.google.com/docs/hosting/full-config
