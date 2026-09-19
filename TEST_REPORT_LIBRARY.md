# 이번 업데이트 검증 결과

대상: `library-20260919-1`, 원본 기반 `appearance-bot-player-upload-autoplay-fixed.zip`의 수정본.

## 실행한 자동 테스트

```text
PYTHONPATH=.:tests python -m unittest test_core test_linked test_split test_new_permissions_auto test_library_update -v
Ran 105 tests in 28.159s
OK (skipped=1)
```

**104개 통과, 1개 건너뜀.** 건너뛴 항목은 단일 작업 폴더 안의 `웹사이트/` 추가 복사본과 원본 웹 파일을 비교하는 기존 테스트입니다. 이번에는 봇/웹 ZIP을 별도 배포하므로 해당 폴더가 없었고, 대신 최종 ZIP 두 개의 static 내용과 설정 파일이 동일한지 패키징 검사로 확인했습니다.

검증된 내용에는 다음이 포함됩니다.

- 실제 로컬 aiohttp HTTP 서버에서 로그인, Bearer/쿠키/CSRF/CORS/권한, 승인/로그아웃/토큰 만료
- 실제 생성한 WAV를 업로드 → 실제 ffprobe 검사 → 해시 확인 → 세 종류 등록 → Store/Assets/WebPanel 재생성 → 재로그인 → 세 종류 조회와 동일 오디오 바이트 반환
- 세 종류에 같은 이름 등록, 각각 독립적인 수정/삭제, 기존 entranceSongs 경로와 webUsers 보존
- 닉네임 변경 시 새 중복 항목이 아닌 이동, 기존 ID/파일 링크 보존, 타순 참조 동시 갱신
- 이름 충돌/사라진 원본의 오래된 수정 요청 거부
- 상황별 노래 연결, 이름 변경 시 경기 사운드 참조 갱신, 삭제 시 기본 연결 복귀
- 파일 없음 상태에서도 목록 유지와 닉네임 변경 가능, 없는 업로드 ID로 신규 곡 등록 차단
- Railway 볼륨 없음/마운트 미확인/경로 불일치에서 업로드 차단
- 임시 Railway 로컬 DB에 곡·일반 계정 저장을 성공으로 처리하지 않음
- 명시한 DATA_DIR를 함부로 옮기지 않음; DATA_DIR 미지정 시 제공된 볼륨 경로 선택
- 관리자용 ZIP 백업에 실제 오디오 포함, 비밀번호/계정/비밀값 미포함, 유실된 파일 ID 안내
- 승인된 재생자의 세 종류 등록/수정/이름 변경 허용 및 관리자 작업 차단
- 사용 중지한 재생자의 기존 세션 무효화

재생 제어 API 테스트의 Discord 객체는 모의 객체입니다. 실제 Discord 통화방 송출 성공을 검증한 것은 아닙니다. 볼륨 마운트 판정의 긍정 테스트는 마운트 함수를 대체해 분기를 검증했습니다. 실제 Railway 볼륨을 생성/연결한 테스트가 아닙니다.

## 브라우저 화면 확인

네트워크를 쓰지 않는 실제 Chromium의 about:blank 문서에 정적 파일을 넣고, 합성 API 응답으로 DOM을 확인했습니다.

`tests/browser_library_offline.py`: **9개 흐름 통과, 미처리 JavaScript 오류 0개.**

로그인 게이트, 수정 가능한 닉네임 입력과 oldName/memberId 전송, 세 종류 탭, 상황곡 선택 UI, 재생자/관리자 버튼 구분, 영구 저장 경고, 로그아웃을 확인했습니다. 화면 너비 360/390/768/1440px에서 가로 넘침이 없었습니다. 스크린샷은 테스트 데이터로 만든 미리보기이며 실제 운영 사이트 화면이 아닙니다.

별도로 두 로컬 HTTP 주소를 이용한 브라우저 통합 테스트를 시도했지만 환경의 브라우저 탐색 정책(`ERR_BLOCKED_BY_ADMINISTRATOR`)으로 시작이 차단됐습니다. 이를 통과한 것으로 계산하지 않았습니다. CORS 등의 서버 동작은 위 실제 로컬 HTTP API 테스트에서 별도로 검증했습니다.

## 코드/패키지 검사

- `python -m compileall -q .` 통과
- `node --check static/app.js` 통과
- `node scripts/check-hosting.cjs` 통과
- 최종 ZIP에서 `firebase.json`, `scripts/check-hosting.cjs`, `static/config.js` 포함 및 올바른 루트 배치 확인
- 봇/웹 ZIP의 static 네 파일, firebase.json, 검사 스크립트 바이트 동일 확인
- `.firebaserc`, 실제 `.env`, 로컬 DB/업로드, 캐시/로그, 폰트 파일을 배포 ZIP에 넣지 않음

## 기본 효과음 보존

처음 제공한 `appeanrace-song-bot-main.zip`의 `sounds/` 파일들과 최종 수정본을 SHA-256으로 비교했습니다.

**52개 모두 동일 · 변경 0개 · 누락 0개.** 기존 파일은 제거하거나 변환하지 않았습니다. ZIP의 `tests/sounds-preservation-current.json`에 해시 목록이 있습니다.

## 확인하지 못한 항목

실제 Firebase/Firestore, 실제 Railway 볼륨과 재배포, 운영 계정 로그인, 실제 Discord 통화방 자동 입장/음성 송출, 실제 YouTube 추출, Docker 이미지 빌드는 검증하지 않았습니다. 서버의 기존 업로드/DB 파일이나 사용자 자격증명에 접근하지 않았습니다.

이번 환경에는 Discord/yt-dlp 런타임 패키지가 없었고 패키지 설치 시 DNS 연결이 실패했습니다. 따라서 원본 `test_voice.py`를 이 실행 묶음에 포함하지 않았고, `main.py`/`player.py`의 전체 런타임 실행을 확인했다고 주장하지 않습니다. 기존 requirements.txt와 voice.py는 변경하지 않았습니다.

따라서 배포 후에는 실제 웹의 **영구 저장 연결됨** 표시를 확인하고, 음원 하나를 등록/저장한 뒤 Railway를 다시 시작해 재로그인·재생을 확인해야 합니다. 이미 사라진 파일의 자동 복구는 제공하지 않습니다.
