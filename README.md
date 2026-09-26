# APPEARANCE — 음악·경기 사운드 컨트롤

현재 수정본: **studio-20260926-1**. **UPDATE_20260926.md를 먼저 읽고 봇 + 웹사이트를 모두 적용하세요.**

자동 등장곡 전체 ON/OFF, 등록/삭제자·재생자·유저 권한, 스테이지 발언자 처리, 세 종류 음악 다중 연결/파일 드롭/순서 편집, 기본 상황 삭제·복원, 원본명 오디오 ZIP, 삼성·아이폰 설치형 웹앱(PWA)을 추가했습니다. 별도 APK/IPA가 아니며 아직 운영 서버에 배포된 결과물이 아닙니다.

계정·데이터·Volume을 유지하세요. 테스트 결과와 미검증 항목은 TEST_REPORT_STUDIO.md에 있습니다.

---

## 아래는 이전 버전 설명입니다. 새 역할·백업·경기 사운드는 위 안내가 우선합니다.

# APPEARANCE — 우리 팀 음악 라이브러리

이번 버전: `library-20260919-1`

**[설치·변경 사항은 MUSIC_LIBRARY_UPDATE.md를 먼저 읽으세요.](MUSIC_LIBRARY_UPDATE.md)**

- 기존 Firebase 웹 + GitHub/Railway 봇 연결 및 계정 유지
- 등장곡 / 응원가 / 상황별 노래 독립 라이브러리
- 라이브러리의 수정 화면에서 닉네임 변경, 등장곡 타순 참조 함께 갱신
- Railway 영구 저장 상태 진단, 볼륨 경로 자동 선택, 임시 저장 업로드 차단
- 원본 오디오 다운로드 및 관리자용 오디오 포함 ZIP 백업

**실제 Railway 볼륨 연결은 코드 배포만으로 생성되지 않습니다.** 기존 볼륨을 보존하거나 봇 서비스에 `/data` 볼륨을 연결하고 `DATA_DIR`를 동일한 경로로 맞추세요. 아직 남아 있는 서버 파일은 배포/볼륨 경로 변경 전에 백업하세요. 이미 없어진 오디오는 원본을 다시 업로드해야 합니다.

봇에는 Python 3.12, FFmpeg/ffprobe와 `requirements.txt` 의존성이 필요합니다. YouTube 링크용 Deno는 기존 Dockerfile에서 설치합니다. 설정은 기존 Railway Variables 또는 비밀 `.env`를 사용하고 공개 저장소에는 넣지 마세요.

```sh
python -m pip install -r requirements.txt
python main.py
```

로컬 테스트:

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
node --check static/app.js
node scripts/check-hosting.cjs
```

Python Discord 라이브러리까지 설치해야 원본 `test_voice.py`를 포함한 전체 검색 테스트를 실행할 수 있습니다. 이번 작업 환경에서 실제로 실행한 테스트 묶음과 미검증 항목은 [TEST_REPORT_LIBRARY.md](TEST_REPORT_LIBRARY.md)에 구분했습니다. 이전 버전의 테스트 보고서는 이번 버전의 검증 결과가 아닙니다.

주요 봇 명령:

```text
!입장 / !퇴장 / !정지 / !진단 / !웹
!재생 이름                     등장곡
!응원가 이름                   응원가
!상황곡 이름                   상황별 노래
!저장 이름 / YouTube주소 / 0:10~0:40
!응원가저장 이름 / YouTube주소 / 0:00~1:00
!상황곡저장 이름 / YouTube주소 / 0:00~0:30
!이름변경 기존이름 / 새이름     기존 등장곡 명령 유지
```

웹 `admin` 로그인과 승인된 재생자 계정을 그대로 사용합니다. 웹의 라이브러리 닉네임과 Discord 실제 닉네임은 별개입니다. 원본 `sounds/` 52개 파일은 바꾸지 않았습니다.
