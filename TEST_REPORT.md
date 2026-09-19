# 연결본 검증 결과 — linked-20260919-1

## 이번 제작 과정에서 실제 수행한 검사

- `PYTHONPATH=.:tests python -m unittest test_core test_split test_linked -v`: **74개 통과**.
- Python `compileall` 및 Node `--check` 구문 검사 통과.
- 웹 배포 사전 검사: `site=appearance-song`, `public=static`, Railway 정확한 주소와 CSP 일치 확인.
- 원본 ZIP의 `sounds/` 파일 52개와 수정본의 52개를 SHA-256으로 비교: 변경 0, 누락 0, 추가 0.
- 기존 로그인 보호본 대비 main/player/voice/storage/assets/validation/requirements 파일 바이트 동일.

## 회귀 테스트에 포함한 중요한 경우

- 실제 사용자가 제공한 Firebase/Railway 주소를 사용한 사전 요청(OPTIONS), Bearer 로그인, 인증된 상태 조회.
- Railway에서 HTTPS를 끝내고 애플리케이션에 HTTP로 넘기는 상황을 Host/Origin 헤더로 재현: Railway 주소에서 로그인하고 Origin 없는 같은 출처 GET 조회 성공.
- 오래된 PUBLIC_URL이 Firebase를 가리키거나 WEB_ORIGINS가 잘못 남아 있어도 연결 프로필이 올바른 주소를 사용함.
- 로그인 전 13개 관리자 라우트에 접근하면 401, 허용되지 않은 웹 출처는 403.
- 틀린 비밀번호, CSRF 누락, 만료된 세션, 로그아웃한 세션, 다른 Origin에 묶인 세션 차단.
- 곡/타순 저장, 업로드한 테스트 WAV의 ffprobe 검사, 인증된 부분 음원 읽기, 메타데이터 백업.
- 웹 제어 요청이 모의 Discord 음성 연결 객체로 전달되는지 확인. 실제 음성 통신은 아님.
- 설정 파일/비밀번호가 없는 경우나 비밀번호가 너무 짧은 경우 안전하게 시작 거부.

## 브라우저/네트워크 검사 제한

Chromium 자동 탐색은 로컬 테스트 주소와 요청 가로채기용 HTTPS 주소 모두 `ERR_BLOCKED_BY_ADMINISTRATOR`로 차단되었습니다.
따라서 이번 실행에서 PC/모바일 화면 검증이나 실제 브라우저 종단간 연결 검증을 통과했다고 주장하지 않습니다.
`browser_split_smoke.py`, `browser_linked_offline.py`는 허용된 별도 테스트 환경에서 다시 실행할 수 있도록 포함했습니다.

실제 공개 Firebase/Railway 페이지 조회도 제작 환경에서 성공하지 못했습니다(웹 도구 fetch 실패 및 실행 환경 DNS 실패).
이를 사용자 서버가 꺼져 있다는 증거로 해석하지 않습니다.
봇 의존성을 새로 설치하려던 작업도 DNS 오류로 완료하지 못했습니다. 따라서 이 실행에서는 Discord 라이브러리가 필요한 `test_voice.py`를 재실행하지 않았으며, 전체 의존성 설치/새 Docker 빌드가 검증됐다고 주장하지 않습니다.
위 74개는 실행 가능한 API/설정/저장소 테스트 모듈만의 실제 통과 수입니다. 이전 대화의 테스트 수를 합산하거나 재사용한 수가 아닙니다.

## 실제 배포 후 확인해야 하는 항목

Firebase Hosting 재배포, Railway GitHub 변경 반영, 기존 WEB_ADMIN_PASSWORD로 실제 로그인, 실제 Firestore 자료 조회, 기존 Discord 통화방 연결/음성 송출, YouTube 추출은 현장 확인이 남아 있습니다.
이 제작 과정에서는 Firebase/Railway 계정에 로그인하거나 배포, 비밀번호 변경, 기존 데이터 변경을 실행하지 않았습니다.
