# 청백전 2봇 기능 검증

- Python compileall: 통과
- `static/app.js` Node 문법 검사: 통과
- Firebase Hosting predeploy 검사: 통과
- 기존 자동 테스트 + 2봇 추가 테스트: 122개 통과, 1개 건너뜀
- 추가 검증: primary/secondary 활성 팀 분리 저장
- 추가 검증: primary/secondary 알림 채널 분리 저장
- 추가 검증: 웹 API가 청팀/백팀 봇 대상을 구분
- 기존 `sounds/`: 52개 파일 SHA-256 비교, 변경 0 / 누락 0 / 추가 0

이 환경에서는 실제 Discord 보조 봇 토큰으로 두 음성 채널에 동시 접속하거나 실제 Railway/Firebase 배포까지 수행하지 않았습니다. 실제 배포 후 두 봇 모두 온라인인지 웹 연결 상태에서 확인하세요.
