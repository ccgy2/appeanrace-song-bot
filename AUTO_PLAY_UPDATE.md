# 자동 등장곡 / 재생자 업로드 수정

## 변경 사항

- 웹 계정 `재생자(player)`도 등장곡 등록/수정, YouTube 링크 등록, 오디오 파일 업로드 가능
- 곡 삭제, 팀 생성/활성화, 타순 변경, 경기 효과음 변경, 회원 관리는 관리자 전용 유지
- Discord 사용자 ID(`memberId`)가 등록된 곡은 해당 사용자가 통화방에 새로 들어오거나 다른 통화방으로 이동하면 자동 재생
- 사용자 ID 방식은 `등장곡 재생인` Discord 역할이나 타순 등록을 요구하지 않음
- 사용자 ID가 없는 기존 곡은 기존 호환성을 위해 `등장곡 재생인 역할 + 타순 + 닉네임` 규칙 유지
- 자동 입장 중복 이벤트 차단 시간을 기존 5초에서 1초로 줄임
- 이벤트의 `after.channel`을 기준으로 처리하여 `member.voice` 캐시 타이밍 때문에 자동 재생이 빠지는 경우를 피함

## 배포

1. 봇 수정본 전체를 기존 GitHub 저장소에 덮어쓰고 Railway 재배포
2. 웹사이트 수정본을 `G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트`에 덮어씀
3. 기존 `.firebaserc`는 유지
4. 해당 웹사이트 폴더에서 `firebase deploy --only hosting`

웹: https://appearance-song.web.app
Railway API: https://appeanrace-song-bot-production.up.railway.app

## 사용자 ID 자동 재생 확인

1. 웹에서 곡을 등록/수정
2. `Discord 사용자 ID`에 실제 사용자 ID 입력
3. 저장
4. 해당 사용자가 통화방에 입장
5. 봇이 다른 사람이 사용 중인 다른 통화방에 이미 들어가 있지 않다면 해당 통화방으로 연결해 자동 재생

ID 등록 방식은 타순에 넣지 않아도 자동 재생됩니다.
