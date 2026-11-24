# 재생 중인데 소리가 나지 않는 문제 수정

버전: `playback-audio-fix-20261124-2`  
작성일: 2026-11-24 (한국 시간)

## 확인한 원인과 수정

앞선 10GB·MP4 수정본의 `player.py`는 FFmpeg에 `-map 0:a:0`을 전달했습니다. 오디오가 여러 개 들어 있는 MP4에서도 첫 번째 트랙만 강제로 선택하는 설정이었습니다.

**첫 번째 오디오는 무음이고 두 번째 기본 오디오에 소리가 있는 MP4**로 재현했습니다. 기존 설정은 정상 길이의 음성 프레임을 계속 내보내고 오류 없이 종료했지만, 모든 샘플이 0이라 소리가 나지 않았습니다. 따라서 화면의 재생 표시와 프레임 개수만으로는 이 문제를 찾지 못했습니다.

이 강제 선택을 제거해 **FFmpeg의 자동 오디오 선택**으로 복원했습니다. 영상 제외 옵션 `-vn`은 유지하므로 Discord에는 음성만 전송합니다. 파일을 변환하거나 원본을 다시 저장할 필요는 없습니다. 이미 등록한 MP4에도 수정된 재생 경로가 적용됩니다.

이는 코드와 합성 MP4로 확인한 회귀 오류입니다. 사용자가 실제로 재생한 파일의 트랙 구조나 운영 Discord 상태까지 확인한 것은 아니므로, 모든 무음 원인이 이 설정이라고 단정하지는 않습니다.

## 함께 보완한 부분

### 봇의 자체 음소거

이미 연결된 봇이 자체 음소거(`self_mute`) 상태이면 다음 연결·재생 요청에서 자체 음소거를 해제하고 Discord 상태 변경을 확인합니다. 해제가 거부되거나 확인되지 않으면 재생 오류를 표시합니다.

서버 관리자가 지정한 **서버 음소거**, 스테이지 **청중/발언자 권한**은 그대로 존중합니다. 봇의 수신 차단 설정(`self_deaf`)도 유지합니다. 자체 음소거가 없는 연결에는 추가 변경 요청을 보내지 않습니다.

### 실제 볼륨과 재생 상태 표시

이전 화면은 마지막에 읽은 팀 볼륨만 표시해, 실제 재생 볼륨이 0%로 바뀐 뒤에도 50%처럼 보일 수 있었습니다.

- 선택한 팀의 곡이 재생 중이면 **현재 재생 소스의 실제 전체 볼륨**을 표시합니다.
- 곡 자체 볼륨 0%, 전체 볼륨 0%, 자체/서버 음소거, 스테이지 청중, 연결 끊김, 일시정지를 구분합니다.
- 실제 재생 중인 봇·통화방·팀을 함께 표시합니다.
- 다른 팀을 선택했다면 볼륨 조절이 선택한 팀에 적용된다는 점을 표시합니다. 다른 팀에서 재생 중인 곡의 볼륨을 몰래 변경하지 않습니다.

현재곡의 음성 프레임이 생성되는 것과 실제 청취자가 소리를 듣는 것은 다릅니다. Discord 사용자별 봇 볼륨, 사용자 출력 장치, 운영 서버의 음성 네트워크 상태까지 웹 화면이 확인하는 것은 아닙니다.

## 적용 순서

1. 수정 파일 ZIP을 풀어 **기존 GitHub 저장소의 같은 경로에 덮어씁니다.** 새 상위 폴더를 추가하지 마세요.
2. Railway 봇을 재배포합니다. 특히 `player.py`, `voice.py`, `release.py`가 반영되어야 합니다.
3. `웹사이트` 폴더의 정적 파일과 `firebase.json`을 반영하고 Firebase Hosting을 배포합니다.
4. 웹사이트를 새로고침하고 진단 화면에서 버전이 **`playback-audio-fix-20261124-2`**인지 확인합니다.
5. 봇을 통화방에서 한 번 퇴장시킨 뒤 다시 연결하고 같은 곡을 재생합니다. 화면에 표시되는 실제 팀과 볼륨도 확인합니다.

Firebase 배포 명령은 `웹사이트` 폴더에서 실행합니다.

```sh
node scripts/check-hosting.cjs
firebase deploy --only hosting
```

**10GB·MP4 등록, 등록/삭제자·재생자의 타순 관리, 경기 사운드 개인 정렬 기능은 유지합니다.** 이번 ZIP에는 해당 변경 파일도 포함합니다. 업로드 설정은 이전 안내와 같이 `MAX_UPLOAD_MB=10240`, `MAX_STORAGE_MB=20480`이며 실제 영구 볼륨의 여유가 필요합니다.

## 실제 검증

- 실제 `discord.py 2.7.1`의 `FFmpegPCMAudio`, `PCMVolumeTransformer`, `VoiceClient.play`, `AudioPlayer`와 네이티브 Opus 인코딩·디코딩을 사용했습니다. 네트워크 전송만 로컬 캡처로 대체했습니다.
- 첫 트랙이 무음인 다중 오디오 MP4에서, 수정 전에는 무음이고 수정 후에는 소리가 있는 PCM과 Opus 데이터가 나오는 것을 확인했습니다. 등장곡의 구간 재생과 경기 사운드의 전체 재생을 모두 확인했습니다.
- 일반 MP3와 단일 오디오 MP4를 `standard`, `economy` 양쪽 설정에서 검사했습니다. 손상 파일은 성공 처리하지 않고 오류가 되는 것도 확인했습니다. 재생 회귀 테스트 4개가 통과했습니다.
- 자체 음소거 복구·확인 지연·거부·시간 초과·서버 음소거 보존의 신규 테스트 13개, 기존 음성 연결 12개, 스테이지 5개, Opus 설정 5개, 기존 FFmpeg/EOF 2개가 통과했습니다.
- 업로드 저장 엔진 23개와 업로드·미디어 HTTP 통합 22개를 다시 실행해 통과했습니다.
- 화면 상태 테스트 및 기존 업로드·타순·정렬 로직도 확인했습니다. 정확한 검증 개수와 패키지 파일 목록은 `tests/package-verification-playback-20261124.json`에 기록합니다.

실제 운영 Discord 음성방의 청취와 Railway/Firebase 배포는 수행하지 않았습니다. 이전 환경에서 Chromium 실행이 제한되어 이번 화면 변경은 Node의 실제 화면 함수 테스트로 검증했으며, 실제 브라우저 조작은 미검증입니다.

프로젝트 의존성과 FFmpeg/ffprobe가 설치된 환경에서 재현합니다.

```sh
python -m unittest discover -s tests -p test_playback_stream_selection.py -v
python -m unittest discover -s tests -p test_voice_self_mute.py -v
python -m unittest discover -s tests -p test_upload_sessions.py -v
python -m unittest discover -s tests -p test_large_upload_api.py -v
node --test tests/test_playback_status.cjs tests/test_resumable_frontend.cjs tests/test_frontend_efficiency.cjs 웹사이트/tests/lineup-permissions.test.cjs 웹사이트/tests/event-order.test.cjs
```

## 적용 후에도 소리가 없을 때

재생 중인 팀의 전체 볼륨을 올리고 Discord에서도 해당 봇의 사용자 볼륨과 서버 음소거를 확인하세요. 여전히 들리지 않으면 **MP3도 같은지, MP4만 그런지**와 **재생을 누른 직후 Railway 로그**를 확인해야 합니다. 이 파일에는 운영 계정, 토큰, 업로드 원본이나 DB를 넣지 않았습니다.

참고: [FFmpeg 스트림 선택 공식 문서](https://ffmpeg.org/ffmpeg.html#Stream-selection), [discord.py 음성 재생 API](https://discordpy.readthedocs.io/en/stable/api.html#discord.VoiceClient.play).
