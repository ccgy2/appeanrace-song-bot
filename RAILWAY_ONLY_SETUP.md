# APPEARANCE — Firebase 없이 Railway 하나로 사용하기

이 버전은 Firebase Firestore를 런타임에서 사용하지 않습니다.

- 웹사이트: Railway
- Discord 봇: Railway
- 회원/곡/타순/경기상황 DB: Railway Volume의 `/data/songs.sqlite3`
- 업로드 MP3: Railway Volume의 `/data/uploads`
- 청팀/백팀 2봇: 그대로 지원
- 경기상황 단곡/랜덤/여러곡 순차: 그대로 지원
- 알림채널 지정: 그대로 지원

## 1. 적용

가장 쉬운 방법은 `appearance-railway-sqlite-full.zip`의 내용을 기존 GitHub 봇 저장소 루트에 덮어쓰는 것입니다.

Railway가 GitHub에 연결되어 있으면 새 커밋을 올린 뒤 자동 배포하거나 Redeploy 하세요.

## 2. Railway Variables

기존 값은 유지하세요.

```text
DATA_DIR=/data
DISCORD_TOKEN=기존 청팀 봇 토큰
DISCORD_TOKEN_SECONDARY=기존 백팀 봇 토큰
WEB_ADMIN_PASSWORD=기존 관리자 비밀번호
```

`FIREBASE_SERVICE_ACCOUNT`가 남아 있어도 이 버전의 기본 저장소는 SQLite라서 런타임에서 사용하지 않습니다.

원하면 아래 값을 명시적으로 추가해도 됩니다.

```text
STORAGE_BACKEND=sqlite
```

Railway Volume의 Mount Path는 기존대로 `/data`를 유지하세요.

## 3. 이제 웹사이트 주소

Firebase deploy를 하지 않습니다.

아래 Railway 주소를 바로 웹사이트로 사용합니다.

```text
https://appeanrace-song-bot-production.up.railway.app
```

Discord에서 `!웹` 명령도 이 주소를 안내합니다.

로그인은 기존과 동일합니다.

```text
아이디: admin
비밀번호: Railway의 WEB_ADMIN_PASSWORD
```

## 4. 기존 Firebase 웹사이트

`https://appearance-song.web.app`은 당장 삭제할 필요 없습니다.
현재 배포된 사이트가 Railway API를 가리키고 있으면 임시로 계속 사용할 수도 있습니다.

하지만 앞으로는 Railway 주소만 사용하면 Firebase Hosting 배포도 필요 없습니다.

## 5. 기존 Firestore 데이터 복구

중요: Firestore quota가 이미 막힌 상태에서는 기존 Firestore 문서를 즉시 읽어서 새 SQLite DB로 가져올 수 없습니다.

이 버전에는 `legacy_migration.py`를 넣었습니다.
Firestore 할당량이 다시 열렸을 때 기존 데이터를 한 번만 안전하게 복사할 수 있습니다.

Railway Variables에 잠깐:

```text
MIGRATE_FIRESTORE_ONCE=true
```

를 추가하고 Redeploy 하세요.

기존 `FIREBASE_SERVICE_ACCOUNT`가 남아 있으면 다음 데이터를 SQLite로 **비파괴 복사**합니다.

- 등장곡
- 응원가
- 상황별 노래
- 타순
- 경기상황
- 팀/서버 설정
- 웹 회원 계정

SQLite에 이미 같은 데이터가 있으면 덮어쓰지 않습니다.

로그에 다음과 비슷하게 나오면 완료입니다.

```text
Firestore 기존 데이터 N개를 Railway SQLite로 비파괴 복사했습니다.
```

완료 후 `MIGRATE_FIRESTORE_ONCE`를 삭제하거나 `false`로 바꾸세요.

업로드한 MP3는 이미 `/data/uploads`에 남아 있으므로, 기존 Firestore 메타데이터가 복구되면 다시 연결될 수 있습니다.

## 6. 주의

Railway Volume 자체를 삭제하지 마세요.
`/data/songs.sqlite3`와 `/data/uploads`가 실제 데이터입니다.

Railway 백업 기능으로 Volume 백업을 잡아두는 것을 권장합니다.
