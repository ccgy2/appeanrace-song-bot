# APPEARANCE STUDIO — 등장곡 봇 + 웹 관리

통화방 연결 처리 수정과 웹 노래 관리 기능을 포함한 교체용 프로젝트입니다.
원본 `appeanrace-song-bot-main.zip`의 효과음과 기존 Firestore 데이터 경로를 유지합니다.

**중요:** 이 압축 파일은 배포 가능한 소스이지, 이미 인터넷에 게시된 사이트가 아닙니다.
봇과 웹 서버가 같은 Python 프로세스에서 실행됩니다. 정적 호스팅에 HTML만 올리면 작동하지 않습니다.
실제 Discord 음성 연결, 실제 Firebase, YouTube 추출 및 Docker 빌드는 제작 환경에서 검증하지 못했습니다.
검증 범위는 `TEST_REPORT.md`에 구분해 적었습니다.

## 1. 기존 봇에서 교체할 때

1. 기존 소스, `.env`, Firebase 설정을 백업하고 기존 봇 프로세스를 종료합니다.
2. 새 폴더 전체를 사용합니다. `main.py`만 교체하면 안 됩니다. `static/`, 나머지 Python 모듈, `sounds/`가 모두 필요합니다.
3. 기존 `DISCORD_TOKEN`, `FIREBASE_SERVICE_ACCOUNT`, `OWNER_ID` 값을 유지합니다. 사용하던 YouTube 쿠키 설정도 필요하면 유지합니다.
4. 웹 로그인용 `WEB_ADMIN_PASSWORD`를 새로 추가합니다. **Discord 토큰이 아니라 별도 관리자 비밀번호**입니다. 12자 이상으로 설정하세요.
5. 시작 명령을 `python main.py`로 맞추고 의존성을 새로 설치/빌드합니다. 예전 `python bot.py` 설정은 제거합니다.
6. 같은 토큰의 봇은 한 곳에서 하나만 실행합니다. 재배포 전 구버전을 반드시 중지하세요.

`FIREBASE_SERVICE_ACCOUNT`가 비어 있으면 새 로컬 SQLite DB를 사용합니다. 기존 곡이 보이지 않는다고 데이터를 지우거나 재등록하기 전에 이 환경변수를 확인하세요.
설정된 Firebase 키가 잘못됐을 때 로컬 DB로 몰래 전환하지는 않습니다.

## 2. Discord 쪽 설정

Discord Developer Portal → 해당 애플리케이션 → Bot → **Message Content Intent**를 켭니다. 이 봇은 `!입장` 같은 접두사 명령어를 사용합니다. [1]
`ENABLE_MEMBERS_INTENT=false`가 기본값입니다. 이를 true로 바꾸는 경우 Server Members Intent도 켜야 합니다.

서버 내 봇 역할과 각 통화방의 권한 덮어쓰기에서 **채널 보기, 연결, 말하기**를 허용하세요.
텍스트 안내에는 메시지 보내기와 링크 임베드 권한이 필요합니다. 역할 부여 명령에는 역할 관리 권한과 올바른 역할 순서가 필요합니다.
봇의 서버 음소거를 해제하고 통화방 인원 제한도 확인하세요. 일반 음성 채널을 지원하며 스테이지 채널은 지원하지 않습니다.

2026년 3월 1일 이후 Discord 음성 통화에는 E2EE/DAVE 지원이 필요합니다. 원본의 discord.py 2.4.0 대신 DAVE 지원이 포함된 2.7.1을 설치하도록 수정했습니다. [2][3]
이 변화는 코드에서 확인한 중요한 호환성 문제이지만, 사용자 서버의 실제 실패 원인을 로그 없이 하나로 확정할 수는 없습니다.

## 3. 로컬 PC에서 실행

Python 3.12와 실제 FFmpeg/ffprobe 실행 파일을 준비하세요.
YouTube 링크 재생에는 Deno도 설치하고 명령줄에서 `deno --version`이 실행되어야 합니다.
`yt-dlp[default]`로 설치하는 JavaScript 지원 구성요소와 Deno는 YouTube 지원에 사용됩니다. [4]
MP3/WAV 직접 업로드 재생은 YouTube 또는 Deno를 거치지 않습니다.

`.env.example`을 `.env`로 복사한 뒤 토큰과 웹 비밀번호를 입력합니다.
기존 Firebase를 계속 쓰는 경우 반드시 기존 서비스 계정 값을 넣습니다.
로컬에서는 `PUBLIC_URL`을 비워 두세요.

Windows PowerShell (프로젝트 폴더에서):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

macOS/Linux:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

실행한 PC의 브라우저에서 `http://localhost:8080`을 열고 **WEB_ADMIN_PASSWORD**로 로그인합니다.
`HOST=0.0.0.0`이면 같은 네트워크에서도 접근할 수 있으므로 방화벽 범위를 제한하세요.
단일 PC에서만 사용할 때는 `.env`의 `HOST=127.0.0.1`로 제한할 수 있습니다.
같은 Wi-Fi의 휴대폰에서 접근하려면 PC의 내부 IP와 포트가 필요합니다. 인터넷 공개는 아래 HTTPS 배포 방식을 사용하세요.

### Docker Compose로 실행

`.env`를 채운 뒤 실행합니다. FFmpeg/ffprobe, libopus, Deno, Python 패키지를 Docker 이미지에 포함하도록 구성했습니다.

```sh
docker compose up -d --build
docker compose logs -f
```

주소는 `http://localhost:8080`입니다. Compose는 안전한 로컬 기본값으로 호스트의 127.0.0.1에만 공개합니다.
종료는 `docker compose down`입니다. **`down -v`는 저장 볼륨까지 삭제하므로 사용하지 마세요.**
Docker에서 Firebase 파일 경로를 쓸 때는 해당 파일을 읽기 전용으로 별도 마운트해야 합니다.
기존 JSON 환경변수 방식은 그대로 사용할 수 있습니다. 실제 키를 Docker 이미지나 Git에 넣지 마세요.

## 4. Railway 등 호스팅에서 웹 공개

기존 봇 호스팅을 그대로 이용할 수 있도록 `Dockerfile`을 제공합니다. Git 저장소에 올릴 때 프로젝트 파일이 빌드 루트에 있도록 하세요.

호스팅 Variables에 다음을 설정합니다.

```dotenv
DISCORD_TOKEN=기존_봇_토큰
FIREBASE_SERVICE_ACCOUNT=기존_서비스계정_JSON
OWNER_ID=본인_Discord_숫자_ID
WEB_ADMIN_PASSWORD=직접_정한_충분히_긴_별도_비밀번호
WEB_ENABLED=true
WEB_ONLY=false
HOST=0.0.0.0
PORT=8080
DATA_DIR=/data
```

`OWNER_ID`는 실제 숫자만 입력합니다. 위 한글 설명은 복사해서 실행하는 값이 아닙니다.
`FIREBASE_SERVICE_ACCOUNT` 역시 실제 기존 JSON 값을 사용합니다.

서버의 웹 포트에 공개 HTTPS 도메인을 연결한 뒤 `PUBLIC_URL=https://실제발급된도메인`을 설정하고 다시 배포합니다.
`PUBLIC_URL`은 주소 안내뿐 아니라 HTTPS 프록시 뒤의 요청 출처 검사와 보안 쿠키 설정에도 쓰입니다. 뒤에 `/download.html` 같은 경로를 붙이지 마세요.
공개 HTTPS 주소를 설정하면 그 주소로 로그인하세요. Secure 쿠키 때문에 HTTP 주소에서 로그인 상태가 유지되지 않을 수 있습니다.

**Railway를 사용하는 경우 볼륨을 `/data`에 마운트하세요.** 볼륨은 재배포 사이에 데이터를 유지하기 위한 저장 공간입니다. [5]
Firebase를 사용해도 **업로드한 오디오 파일은 `/data/uploads`에 있으므로 볼륨이 필요**합니다.
볼륨 용량보다 작은 `MAX_STORAGE_MB`를 설정하고 여유 공간을 남기세요. 기본값은 1024MB입니다.
같은 봇은 한 프로세스/한 replica로만 실행합니다. 웹과 봇을 서로 다른 replica로 나누지 마세요.
자동 절전/서버리스처럼 프로세스가 자주 중단되는 환경은 지속적인 음성 접속에 맞지 않습니다.

`GET /healthz`는 웹 서버 생존 확인용입니다. 200 응답이 Discord 음성 접속 성공을 뜻하지는 않습니다.
본문의 `discord` 값과 웹 진단 탭도 확인하세요.
Discord 음성은 웹 HTTP 포트와 별도로 UDP 송수신을 사용하므로, 호스팅 방화벽/NAT의 UDP 통신 조건도 충족해야 합니다. [2]

## 5. 웹에서 노래 등록

로그인 → 관리할 팀 선택 → **선수 이름 + YouTube 링크 + 시작/끝 시간** → 등장곡 저장.
예: 시작 `0:10`, 끝 `0:40`이면 10초부터 30초간 재생합니다. 시간은 초, `분:초`, `시:분:초`를 지원합니다.
YouTube는 공개 영상의 개별 링크를 사용하세요. 재생목록 일괄 등록/음악 제목 검색은 포함하지 않았습니다.
링크를 등록할 때 미리 YouTube 접속을 강제하지 않으며, 실제 재생할 때 오디오 주소를 조회합니다.

**파일 업로드**를 선택하면 MP3, WAV, M4A, OGG, FLAC, AAC, OPUS, WEBM을 서버로 올릴 수 있습니다.
확장자뿐 아니라 ffprobe로 오디오 유효성과 길이를 검사합니다. 기본 파일당 25MB, 전체 업로드 1024MB입니다.
업로드 후 구간을 확인하고 반드시 **등장곡 저장**을 눌러야 선수와 연결됩니다.
직접 업로드한 오디오는 브라우저에서 미리 들을 수 있습니다. 목록의 **재생 / 통화방 5초**는 선택한 Discord 통화방으로 재생하는 버튼입니다.

기존 곡의 수정/삭제, 선수 검색, 1~9번 타순, 다음 타자, 통화방 연결/퇴장, 정지, 볼륨, 15개 경기 사운드 교체를 지원합니다.
타순은 드롭다운에서 고른 뒤 **타순 저장**을 눌러야 반영됩니다.
관리할 팀을 바꿔도 현재 Discord 경기 팀은 자동으로 바뀌지 않습니다. **이 팀으로 경기 진행**을 눌러 활성화하세요.
새 곡과 설정은 봇을 재시작하지 않고 사용할 수 있습니다.

자동 입장곡 조건은 기존과 같이 **등장곡 재생인 역할 + 해당 팀 타순 등록**입니다.
기본적으로 선수 이름과 서버 닉네임을 비교합니다. 웹의 선택 설정에서 Discord 사용자 ID를 연결하면 닉네임이 달라도 찾을 수 있습니다.

## 6. 주요 Discord 명령어

| 명령어 | 동작 |
|---|---|
| `!입장` | 사용자가 있는 일반 음성 채널로 접속 |
| `!퇴장`, `!정지` | 음성 채널 퇴장 / 현재 및 준비 중 재생 취소 |
| `!진단`, `!웹`, `!도움` | 설치·권한 진단 / 관리 주소 / 사용법 |
| `!팀 팀명` | 현재 경기 팀 변경 |
| `!저장 이름 / YouTube주소 / 0:10~0:40` | 곡 저장. `!변경`도 같은 형식 |
| `!재생 이름`, `!미리듣기 이름` | 등록된 곡 재생 / 5초 재생 |
| `!타순 1 / 이름`, `!교체 1 / 이름` | 타순 지정 |
| `!라인업` | 타순과 경기 사운드 버튼 표시 |
| `!볼륨 50` | 볼륨 0~100. 재생 중인 곡에도 적용 |
| `!등장곡역할주기 @유저`, `!등장곡역할회수 @유저` | 관리자 또는 OWNER_ID 전용 역할 관리 |
| `!이름변경 새이름` | 본인 닉네임에 해당하는 곡/타순 이름 변경 |
| `!이름변경 기존이름 / 새이름` | 관리자 권한으로 이름 변경 |
| `!이벤트저장 키 파일명` | 기존 sounds 폴더의 효과음 선택 |

곡·타순·재생 조작은 서버 관리자, 등장곡 재생인 역할, OWNER_ID가 사용할 수 있습니다.
권한이 없는 사람은 라인업 버튼으로도 곡을 바꿀 수 없습니다.
예전 메시지의 버튼은 새 custom_id가 없으므로 교체 후 **`!라인업`을 한 번 새로 실행**하세요.
새 메시지 버튼은 봇 재시작 후에도 다시 연결되도록 등록됩니다.

## 7. 연결이 안 될 때 확인 순서

먼저 노래 없이 `!입장`으로 통화방 접속만 시험하세요. 연결 실패와 YouTube 재생 실패는 서로 다른 문제입니다.

| 증상 | 확인할 부분 |
|---|---|
| 봇 자체가 로그인하지 못함 | DISCORD_TOKEN, Message Content Intent, 시작 명령, 실행 로그 |
| 4017 / davey 관련 오류 | 새 requirements 설치 또는 Docker 캐시 없이 재빌드; 구버전 프로세스 종료 |
| 연결 시간 초과 | UDP 송수신, 방화벽/NAT, 같은 토큰의 중복 실행 |
| 권한 부족 / 방이 가득 참 | 채널별 보기·연결·말하기 허용, 인원 제한 |
| 방에는 들어왔지만 무음 | 서버 음소거, 볼륨, FFmpeg, 원본 파일, 업로드 파일 테스트 |
| YouTube만 실패 | 영상 공개 여부, 호스팅 IP 제한, Deno/yt-dlp-ejs 설치, 직접 업로드와 비교 |
| 웹 로그인이 반복됨 | PUBLIC_URL과 실제 접속 주소의 HTTPS/HTTP 일치, 쿠키 설정 |
| 재배포 후 파일 없음 | DATA_DIR과 볼륨 마운트 경로 일치 여부 |

로그를 공유할 때 Discord 토큰, Firebase 개인 키, YouTube 쿠키는 가리세요.
YouTube의 서버 IP 차단이나 영상의 접근 제한을 이 코드가 해제해 주지는 않습니다.
본인이 사용할 권리가 있는 음원만 등록하세요.

## 8. 데이터와 보안 범위

기존 경로 `teams/{팀}/entranceSongs`, `lineup`, `state`, `events`를 유지합니다.
**기존 구조를 보존했으므로 같은 이름의 팀은 봇이 들어간 모든 Discord 서버에서 공유됩니다.** 서버별 독립 데이터가 필요하면 서로 다른 팀 이름을 사용하세요.
서버의 선택 팀은 새로 `guilds/{서버ID}`에 저장합니다. 같은 Firebase 프로젝트를 쓰면 기존 곡·타순을 읽도록 설계했습니다. 실제 기존 프로젝트 연동 테스트는 별도입니다.

이 웹은 **봇 소유자/신뢰할 수 있는 운영진을 위한 단일 관리자 도구**입니다.
웹 비밀번호를 아는 사람은 봇이 들어간 모든 서버와 팀을 관리할 수 있습니다. 일반 회원용 공개 신청 사이트나 Discord OAuth 로그인 사이트는 아닙니다.
세션 쿠키, 요청 출처 검사, CSRF 토큰, 로그인 시도 제한, 업로드 용량/형식 검사, 파일 경로 제한을 넣었습니다.
공개 배포에는 HTTPS를 사용하고 `.env`, 계정 키, cookies 파일을 공개 저장소에 올리지 마세요.
웹 비밀번호 변경은 프로세스 재시작 후 적용됩니다. 재시작 시 웹 세션도 만료됩니다.

웹의 백업 버튼은 **곡·타순 등 JSON 메타데이터만** 저장합니다. 오디오와 로컬 DB를 보존하려면 봇을 중지한 상태에서 DATA_DIR 전체도 따로 백업하세요.
이 버전에는 JSON 백업 가져오기 UI가 없습니다. Firebase 기존 데이터의 사전 백업은 사용 중인 프로젝트의 백업 절차를 따르세요.

곡 삭제/효과음 교체는 다른 곡의 파일 공유를 고려해 원본 업로드 파일을 자동 삭제하지 않습니다.
미사용 업로드도 용량 제한에 포함됩니다. 정리가 필요하면 메타데이터 백업을 확인하고 아무 곡/효과음도 참조하지 않는 ID의 오디오 및 같은 ID.json만 수동으로 정리하세요.
업로드 파일은 공유 링크 없이 로그인한 관리자에게만 제공됩니다.

## 9. 검증과 업데이트

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

테스트에는 실제 HTTP API/SQLite/ffprobe 검사와, Discord를 대체한 모의 객체로 수행하는 연결 로직 검사가 섞여 있습니다. 실제 Discord 연결 성공을 뜻하지 않습니다.
제작 환경의 통과 결과와 미검증 범위는 `TEST_REPORT.md`를 참고하세요.

YouTube 호환성 업데이트가 필요하면 requirements.txt의 yt-dlp 버전을 검토해 변경한 뒤 다시 설치/빌드하세요.
현재 파일은 확인한 안정 릴리스 `2026.8.19`를 지정했습니다. [6]
실행 중인 봇 안에서 패키지를 자동 업데이트하지는 않습니다.

## 참고한 공식 문서 (2026-09-19 확인)

[1] discord.py Gateway Intents: https://discordpy.readthedocs.io/en/stable/intents.html

[2] Discord Voice / DAVE / UDP: https://docs.discord.com/developers/topics/voice-connections

[3] discord.py Changelog: https://discordpy.readthedocs.io/en/stable/whats_new.html

[4] yt-dlp 공식 설치/의존성 안내: https://github.com/yt-dlp/yt-dlp

[5] Railway Volumes: https://docs.railway.com/volumes/reference

[6] yt-dlp PyPI: https://pypi.org/project/yt-dlp/

Deno Docker 바이너리 복사 방식: https://github.com/denoland/deno_docker
