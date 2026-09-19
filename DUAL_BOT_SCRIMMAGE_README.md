# 청백전 2봇 / 2통화방 모드

한 Discord 봇 계정은 같은 서버에서 두 음성 채널에 동시에 들어갈 수 없으므로, 기존 봇 계정 + 보조 봇 계정 1개를 한 Railway 프로세스에서 같이 실행합니다.

## 동작 구조

- 기존 `DISCORD_TOKEN` = 청팀 봇(1번 봇)
- `DISCORD_TOKEN_SECONDARY` = 백팀 봇(2번 봇)
- Railway 서비스는 기존 것 하나만 사용합니다.
- Firebase 웹사이트도 기존 `https://appearance-song.web.app` 하나만 사용합니다.
- 기존 Firebase 음악 라이브러리와 Railway Volume `/data`를 두 봇이 같이 사용합니다.
- 웹에서 `재생 봇`을 청팀 봇 / 백팀 봇으로 바꿔 각자 다른 통화방에 연결할 수 있습니다.
- 각 봇의 현재 경기 팀은 따로 저장됩니다. 청팀 봇=청팀, 백팀 봇=백팀처럼 설정할 수 있습니다.
- 2봇 모드에서 자동 등장곡은 각 봇에 지정된 현재 경기 팀만 검사합니다. 청/백 봇이 같은 선수를 동시에 잡는 문제를 줄입니다.

## 1. 보조 Discord 봇 만들기

Discord Developer Portal에서 기존 봇과는 별개의 Application/Bot을 하나 만드세요.

보조 봇의 Bot 설정에서 `Message Content Intent`를 켜세요. 웹에서만 조작하더라도 코드가 이 intent를 요청하므로 켜두는 것이 안전합니다.

보조 봇을 기존 Discord 서버에 초대하고 최소 다음 권한을 주세요.

- 채널 보기
- 메시지 보내기
- 링크 임베드
- 음성 채널 연결
- 말하기

보조 봇 토큰은 채팅이나 GitHub에 올리지 마세요.

## 2. Railway Variables

기존 Railway 봇 서비스의 Variables에 보조 봇 토큰만 추가합니다.

```dotenv
DISCORD_TOKEN_SECONDARY=여기에_보조_봇_토큰
```

원하면 웹 표시 이름도 바꿀 수 있습니다.

```dotenv
PRIMARY_BOT_LABEL=청팀 봇
SECONDARY_BOT_LABEL=백팀 봇
```

기존 값은 그대로 유지합니다.

```text
DISCORD_TOKEN
FIREBASE_SERVICE_ACCOUNT
WEB_ADMIN_PASSWORD
OWNER_ID
DATA_DIR=/data
```

`DISCORD_TOKEN_SECONDARY`에는 기존 `DISCORD_TOKEN`과 같은 토큰을 넣으면 안 됩니다.

새 Railway 서비스를 만들거나 replica를 2개로 늘릴 필요가 없습니다. 기존 서비스 한 개 / replica 1개에서 두 Discord 계정을 실행합니다.

## 3. 봇 패치 적용

`appearance-bot-dual-voice-patch.zip`의 파일을 기존 GitHub 봇 프로젝트 루트에 덮어쓰고 Railway를 재배포합니다.

변경 파일:

- `config.py`
- `main.py`
- `storage.py`
- `webapp.py`

기존 `sounds/`, 음악 파일, Firebase 데이터, Volume은 건드리지 않습니다.

## 4. 웹사이트 패치 적용

`appearance-website-dual-voice-patch.zip`의 파일을 아래 기존 웹사이트 폴더에 덮어씁니다.

```text
G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트
```

변경 파일:

- `static/index.html`
- `static/app.js`

기존 `.firebaserc`, `firebase.json`, `static/config.js`는 그대로 유지합니다.

그 다음:

```bat
cd /d "G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트"
firebase deploy --only hosting
```

## 5. 청백전 시작 방법

1. 웹사이트 로그인
2. 상단 `재생 봇`에서 **청팀 봇** 선택
3. 서버 선택 → 청팀 선택 → `이 봇의 경기 팀으로 설정`
4. 청팀 통화방 선택 → `연결`
5. `재생 봇`에서 **백팀 봇** 선택
6. 같은 서버 → 백팀 선택 → `이 봇의 경기 팀으로 설정`
7. 백팀 통화방 선택 → `연결`

이후 두 봇은 같은 Discord 서버 안에서 서로 다른 통화방에 동시에 머물 수 있습니다.

## Discord 명령어

기존 청팀 봇은 예전과 같습니다.

```text
!입장
!퇴장
!정지
!팀 청팀
!알림채널설정 #채널
```

보조 백팀 봇은 명령어 충돌을 막기 위해 `!2` 접두사를 사용합니다.

```text
!2입장
!2퇴장
!2정지
!2팀 백팀
!2알림채널설정 #채널
!2도움
```

웹에서 조작할 때는 명령어 접두사를 신경 쓸 필요가 없습니다.

## 저장

- 음악 라이브러리: 기존 Firebase 데이터 공유
- 업로드 오디오: 기존 Railway Volume `/data` 공유
- 청팀/백팀 봇의 활성 팀: 서로 따로 저장
- 청팀/백팀 알림 채널: 서로 따로 저장

일반적인 Railway Deploy/Redeploy를 해도 기존 Volume을 삭제하지 않는 한 `/data`의 업로드 음원은 유지됩니다.
