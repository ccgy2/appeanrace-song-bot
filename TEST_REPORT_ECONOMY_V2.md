# 2차 절약형 검증 결과

버전: `resource-saving-20261025-2` · 2026-10-25

## 결과 요약

| 항목 | 결과 |
| --- | --- |
| Python 전체 | 252개 실행: 244개 통과, 기존 사유로 8개 건너뜀, 실패 0 |
| 1차 프런트엔드 회귀 | 13개 통과 |
| 새 진단 화면 브라우저 | 14개 통과 |
| 기존 Studio 브라우저 | 17개 통과 |
| 실제 로컬 웹/API 브라우저 | 15개 통과 |
| 브라우저 JavaScript 페이지 오류 | 0 |
| Python/JavaScript 문법 | 통과 |
| Firebase Hosting 파일·연결 주소 검사 | 통과 |

기존에 건너뛰던 8개는 원본에 없는 과거 `setup_deploy.py` 관련 테스트입니다. 기존 테스트를 지우거나 건너뛰도록 수정하지 않았습니다. 추가 Python 검증은 자원 계측 17개, 인증 자원 상한 10개, 음성 프로필 5개, 실제 통합 5개입니다.

## 음성 절약 프로필

`standard`는 기존 Discord 호출의 기본값을 보존합니다. `economy`는 공개 `VoiceClient.play` 옵션으로 96kbps/FEC 끔/음악 신호/full 대역을 지정합니다. 비공개 Opus 상태나 ctypes를 수정하지 않습니다.

실제 libopus로 동적으로 음량이 달라지는 PCM을 인코딩하고 디코딩했습니다. 기존 테스트의 곡별 음량·마스터 음량·자동 등장곡·두 봇 처리도 통과했습니다. 30초 음원 각 5회 벤치마크에서 1,500프레임을 모두 디코딩했고, 인코딩 CPU 중앙값은 1.000305초에서 0.200641초, 패킷 본문은 447,312바이트에서 367,909바이트로 줄었습니다.

조건: Python 3.12.14, discord.py 2.7.1, libopus 1.4, 제공된 `sounds/homerun.mp3`, 마스터 음량 50%. FFmpeg 디코딩은 별도 중앙값 0.144687 CPU초입니다. 결과와 재현 스크립트는 `tests/audio-profile-measurements.json`, `tests/benchmark_audio_profiles.py`입니다.

**청감 품질 동등성, 실제 네트워크 손실 상황, Discord 전송 비용 또는 월 청구액 감소율은 측정하지 않았습니다.** 경제형의 패킷 손실 보정 해제는 불안정한 네트워크에서 끊김을 늘릴 수 있으며 `AUDIO_PROFILE=standard`로 복원할 수 있습니다.

## 계측·접근 권한

- Linux /proc와 cgroup v1/v2, 네임스페이스, 접근 실패, 잘못된 카운터, CPU 차분, 5초 캐시를 fixture로 검증했습니다.
- 실제 로컬 자식 프로세스가 나타나고 종료 뒤 사라지는 것을 확인했습니다.
- 읽기 범위와 결과는 현재 봇과 그 자손에 한정하며, 조회량을 제한했습니다. 결과에 명령행·환경변수·PID·토큰을 넣지 않습니다.
- API 통합 테스트에서 관리자에게만 추가 자원 정보가 제공되고, 일반 계정 요청은 계측을 실행하지 않는 것을 확인했습니다. 기존 인증·CORS·CSRF·API no-store 정책을 유지합니다.
- 공개 연결 확인에는 비밀 없는 `release`만 추가했습니다. 과거 클라이언트와 테스트의 `build` 값은 호환성을 위해 보존합니다.

## 인증 처리 자원

만료 세션/IP 기록 정리, 활성 세션 보존, 한도 초과 시 429와 재시도 안내, 가입/로그인 제한 분리, 기존 310,000회 해시 강도, 해시 동시성·대기열·취소 처리를 검증했습니다. 일반 사용자의 저장 내용과 권한은 변경하지 않습니다. 실제 운영 비용 상승이 인증 요청 때문이었다고 확인한 것은 아닙니다.

## 브라우저 범위

새 진단 화면은 실제 Chromium에서 standard/economy 각각 320·390·1440px로 확인했습니다. 첫 CPU 값의 대기 안내, 5초 이상 뒤 수치 표시, 관리자 전용 결과, NaN/undefined 미표시, 페이지 오류와 가로 넘침을 검사했습니다.

Studio 17개는 모형 API/업로드/설치 이벤트를 쓰는 실제 Chromium DOM 검사입니다. 별도 출처 15개는 실제 로컬 aiohttp·SQLite·ffprobe를 사용하며 Discord 전송은 모형입니다. 과거 브라우저 테스트의 누락된 관리자 아이디, 기본 팀 가정, 비동기 대기 및 도구의 CSP 충돌은 임시 실행기에서만 보완했습니다. 제품 CSP를 낮추거나 기존 테스트를 수정하지 않았습니다.

캡처 이미지는 검증에만 사용하고 ZIP에 포함하지 않았습니다.

## 미검증·운영 적용

- 실제 Railway 배포·컨테이너 빌드·Volume·과금 전후 비교
- 실제 Discord 로그인·음성 전송·DAVE·스테이지·네트워크 손실
- 실제 YouTube/Deno 추출 및 제공자 IP·쿠키 제한
- 실제 Firebase Hosting 배포와 Firestore 외부 연결
- 실제 휴대전화 PWA 업데이트와 완전 오프라인 진입

운영 계정에는 접속하거나 설정을 변경하지 않았습니다. 새 버전을 배포한 뒤 실제 재생을 확인하고, Railway Usage의 서비스별 Memory/CPU/Egress 금액으로 원인을 판단해야 합니다. 현재 순간 사용량이나 로컬 부분 벤치마크를 월 비용 예측으로 사용하지 마세요.

## 재현

```sh
python -m unittest discover -s tests -v
node tests/test_frontend_efficiency.cjs
python tests/benchmark_audio_profiles.py --repeats 5
node --check static/app.js
```

Hosting 검사는 `웹사이트/` 폴더에서 `node scripts/check-hosting.cjs`를 실행합니다.
