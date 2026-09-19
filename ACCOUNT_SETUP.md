# 웹 회원/재생자 계정 설정

- Firebase 웹: `https://appearance-song.web.app`
- Railway API: `https://appeanrace-song-bot-production.up.railway.app`
- 기본 관리자 아이디: `admin`
- 최초 관리자 비밀번호: Railway Variables의 기존 `WEB_ADMIN_PASSWORD`

## 권한
- `admin`: 모든 기능 + 재생자 관리
- `player`: 곡/타순 조회, 통화방 연결·퇴장, 곡/타순/효과음 재생, 정지, 볼륨 조절
- `pending`: 회원가입 직후 상태. 로그인 불가. 관리자가 승인해야 함.

## 사용 순서
1. 봇 수정본을 기존 GitHub 저장소에 덮어쓰고 Railway 재배포.
2. 웹사이트 수정본의 내용을 `G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트`에 덮어쓰기. 기존 `.firebaserc`는 유지.
3. 웹사이트 폴더에서 `firebase deploy --only hosting`.
4. `https://appearance-song.web.app`에서 `admin` + 기존 WEB_ADMIN_PASSWORD로 로그인.
5. 사용자는 회원가입. 관리자는 왼쪽 `재생자 관리`에서 `재생자로 승인`.

계정은 봇이 사용하는 Firebase/Firestore에 `webUsers` 컬렉션으로 저장됩니다. 비밀번호 원문은 저장하지 않고 PBKDF2-SHA256 해시만 저장합니다.

주의: 최초 admin 계정이 생성된 뒤에는 Railway의 WEB_ADMIN_PASSWORD만 바꿔도 이미 생성된 admin 계정 비밀번호가 자동 변경되지는 않습니다.
