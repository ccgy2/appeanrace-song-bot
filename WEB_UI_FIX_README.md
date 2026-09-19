# Railway 웹 UI 복구 패치

이번 문제는 봇/DB 문제가 아니라 최신 Railway 전체본의 HTML 병합 누락입니다.

복구 내용:
- `재생 봇` 선택칸 복구 (청팀 봇 / 백팀 봇)
- Discord 서버/팀 목록 로딩 중 JS 중단 문제 해결
- 통화방 영역에 현재 선택한 봇 이름 표시
- 경기 상황 추가 시 `단곡 / 랜덤 / 여러곡 순차` 선택칸 복구
- 브라우저가 이전 정적 파일을 캐시하지 않도록 버전 쿼리 추가

적용:
1. ZIP 안의 `static/index.html`을 현재 GitHub 저장소의 `static/index.html`에 덮어쓰기
2. GitHub에 commit/push
3. Railway Redeploy
4. 브라우저에서 Ctrl+F5로 강력 새로고침

봇 Python 파일과 Railway Variables는 수정할 필요 없습니다.
