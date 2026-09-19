# 경기 상황 다중 곡 / 재생 방식 업데이트

## 재생 방식
- 단곡: 목록 첫 곡을 항상 재생
- 랜덤: 등록된 여러 곡 중 한 곡을 매번 무작위 재생
- 여러곡 순차: 상황 재생 버튼을 누를 때마다 다음 곡으로 넘어가며 마지막 뒤에는 첫 곡으로 돌아감

한 경기 상황에 최대 30곡까지 연결할 수 있습니다.
`상황별 노래` 라이브러리 곡과 직접 업로드 오디오를 함께 넣을 수 있습니다.

## 적용
### 봇
`appearance-bot-event-modes-patch.zip`의 `player.py`, `webapp.py`, `storage.py`를 기존 GitHub 봇 프로젝트 루트에 덮어쓴 뒤 Railway를 재배포합니다.

### 웹사이트
`appearance-website-event-modes-patch.zip`의 `static` 폴더를 기존
`G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트`
안에 덮어씁니다. 기존 `.firebaserc`, `firebase.json`, `static/config.js`는 그대로 유지됩니다.

그 후:

```bat
cd /d "G:\7. 디스코드 봇\appeanrace-song-bot-main\웹사이트"
firebase deploy --only hosting
```

## 기존 데이터
기존 단일 경기 사운드 설정도 계속 읽습니다. 새 웹에서 곡 목록을 저장하면 새 다중 곡 형식으로 전환됩니다.
상황별 노래 이름을 수정하면 경기 상황의 다중 곡 목록 안 참조도 함께 변경됩니다.

## 검증
- 자동 테스트: 125개 통과, 1개 건너뜀
- Python compileall 통과
- `static/app.js` Node 문법 검사 통과
- 최초 원본 `sounds/` 52개 SHA-256 비교: 변경/누락 0개
- 실제 Railway/Firebase 배포 및 Discord 음성 송출은 이 환경에서 수행하지 않음
