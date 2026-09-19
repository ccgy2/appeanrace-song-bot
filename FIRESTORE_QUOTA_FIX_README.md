# Firestore 할당량 초과 수정

## 원인
웹의 자동 새로고침이 `/api/state`를 주기적으로 호출하면서
곡/타순/경기상황/팀 정보를 Firestore에서 계속 다시 읽었습니다.

Railway 로그의 `429 Quota exceeded` / `RESOURCE_EXHAUSTED`는
Discord 봇이나 Railway 웹 서버가 죽은 것이 아니라 Firestore 조회가 거부된 상태입니다.

## 이번 수정
### 봇 서버 `webapp.py`
- `/api/live` 추가
- 실시간 Discord 음성 연결/현재곡/다른 봇 통화방 정보는 Firestore를 읽지 않음
- `/api/state`의 Firestore-backed 부분을 5분 캐시
- 웹에서 저장/수정/삭제/제어를 하면 캐시 즉시 무효화
- Firestore quota 오류를 웹에 명확하게 표시

### Firebase 웹 `static/app.js`
- 기존 15초마다 `/api/state` 전체 조회 제거
- 10초마다 `/api/live`만 조회
- `/api/live`는 Firestore 읽기 0회
- 로그인/팀변경/저장 같은 실제 변경 때만 `/api/state` 전체 조회

## 적용
### 1. 봇
`appearance-bot-firestore-quota-fix.zip` 안의 `webapp.py`를
현재 GitHub 봇 프로젝트 루트의 `webapp.py`에 덮어쓰고 Railway 재배포.

### 2. 웹
`appearance-website-firestore-quota-fix.zip` 안의 `static/app.js`를
`G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트\static\app.js`
에 덮어쓰기.

그 후:
```bat
cd /d "G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트"
firebase deploy --only hosting
```

## 현재 이미 quota가 소진된 경우
이 수정은 앞으로 반복 소진되는 것을 막는 수정입니다.
이미 Firestore가 429를 내고 있으면 첫 전체 상태 조회는 quota가 다시 열릴 때까지 실패할 수 있습니다.
Billing을 켜지 않는 경우 Firestore의 일일 무료 quota 갱신을 기다린 뒤 다시 접속하세요.

기존 Firebase 데이터, Railway `/data` Volume, 음악 파일은 변경하지 않습니다.
