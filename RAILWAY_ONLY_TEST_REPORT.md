# Railway + SQLite 버전 검증

- Python compileall: 통과
- JavaScript `node --check static/app.js`: 통과
- 자동 테스트: 120개 통과, 1개 건너뜀
- SQLite를 기본 저장소로 사용: 확인
- `FIREBASE_SERVICE_ACCOUNT`가 남아 있어도 기본 SQLite에서 무시: 테스트 통과
- 두 봇이 동일 SQLite 파일을 사용할 때 DB 호출을 프로세스 공용 lock으로 직렬화
- SQLite WAL / busy timeout 적용
- 기존 `sounds/` 파일: 52개
- 기존 버전 대비 변경: 0개 / 누락: 0개
- 실제 Railway 배포 및 실제 Discord 음성 송출은 이 환경에서 수행하지 않음
