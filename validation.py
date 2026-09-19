"""Discord 명령어와 웹 API에서 공유하는 검증."""
from __future__ import annotations
import math
import re
from urllib.parse import parse_qs, urlparse

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.opus', '.webm'}
EVENTS = {
    'lineup': '라인업 송', 'homerun': '홈런', 'homerun2': '홈런 2',
    'strikeout': '삼진', '4ball': '볼넷', 'fullcount': '풀카운트',
    'look': '견제', 'fly': '플라이', 'out': '아웃', 'steal': '도루 성공',
    'inning_change': '이닝 교대', 'start': '경기 시작',
    'game_end1': '경기 종료', 'game_end': '경기 종료 랜덤', 'score': '득점',
}


SONG_CATEGORIES = {'entrance': '등장곡', 'cheer': '응원가', 'situation': '상황별 노래'}
SONG_COLLECTIONS = {'entrance': 'entranceSongs', 'cheer': 'cheerSongs', 'situation': 'situationSongs'}


def song_category(value: object = 'entrance') -> str:
    if not isinstance(value, str) or value not in SONG_CATEGORIES:
        raise ValueError('곡 종류는 등장곡·응원가·상황별 노래 중에서 선택하세요.')
    return value


def clean_name(value: object, label: str = '이름') -> str:
    text = str(value or '').strip()
    if not 1 <= len(text) <= 64 or any(ord(c) < 32 for c in text):
        raise ValueError(f'{label}은 1~64자의 텍스트여야 합니다.')
    if '/' in text or '\\' in text or text in {'.', '..'} or (text.startswith('__') and text.endswith('__')):
        raise ValueError(f'{label}에 /, \\ 또는 예약된 이름을 사용할 수 없습니다.')
    return text


def seconds(value: object) -> float:
    text = str(value).strip()
    try:
        bits = text.split(':')
        if not 1 <= len(bits) <= 3 or any(not b for b in bits):
            raise ValueError
        vals = [float(b) for b in bits]
        if len(vals) > 1 and (any(v < 0 or v >= 60 for v in vals[1:]) or any(not v.is_integer() for v in vals[:-1])):
            raise ValueError
        result = sum(n * 60 ** i for i, n in enumerate(reversed(vals)))
        if not math.isfinite(result) or result < 0 or any(v < 0 for v in vals):
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise ValueError('시간은 30, 1:20, 01:02:30처럼 입력하세요.') from None


def time_range(start: object, end: object) -> tuple[float, float]:
    a, b = seconds(start), seconds(end)
    if a > 86400 or b <= a or b - a > 3600:
        raise ValueError('끝 시간은 시작보다 뒤여야 하며, 재생 구간은 최대 1시간입니다.')
    return a, b


def youtube_url(value: object) -> str:
    """임의 URL 추출을 막고 YouTube 영상 주소만 허용한다 (SSRF 방지)."""
    url = str(value or '').strip()
    try:
        p = urlparse(url)
        if p.scheme not in {'https', 'http'} or p.username or p.password or p.port not in {None, 80, 443}:
            raise ValueError
        host = (p.hostname or '').lower()
        parts = p.path.strip('/').split('/')
        if host in {'youtu.be', 'www.youtu.be'}:
            video_id = parts[0]
        elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
            if p.path == '/watch':
                video_id = parse_qs(p.query).get('v', [''])[0]
            elif len(parts) == 2 and parts[0] in {'shorts', 'live', 'embed'}:
                video_id = parts[1]
            else:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError
        return 'https://www.youtube.com/watch?v=' + video_id
    except (ValueError, IndexError):
        raise ValueError('올바른 YouTube 영상 링크를 입력하세요. 그 외 주소는 허용하지 않습니다.') from None


def member_id(value: object) -> str:
    text = str(value or '').strip()
    if text and not re.fullmatch(r'[0-9]{15,22}', text):
        raise ValueError('Discord 사용자 ID는 15~22자리 숫자입니다. 모르면 비워두세요.')
    return text


def validate_song(data: dict) -> dict:
    category = song_category(data.get('category', 'entrance'))
    name = clean_name(data.get('name'), '닉네임 / 곡 이름')
    a, b = time_range(data.get('start', 0), data.get('end', 30))
    source = data.get('source', 'youtube')
    out = {'name': name, 'category': category, 'start': a, 'end': b, 'memberId': member_id(data.get('memberId')), 'source': source}
    if source == 'youtube':
        out['url'] = youtube_url(data.get('url'))
    elif source == 'upload':
        asset = str(data.get('assetId', ''))
        if not re.fullmatch(r'[a-f0-9]{32}', asset):
            raise ValueError('먼저 오디오 파일을 업로드하세요.')
        out['assetId'] = asset
    else:
        raise ValueError('지원하지 않는 곡 형식입니다.')
    return out


def parse_song_args(args: str) -> dict:
    try:
        name, url, times = [p.strip() for p in args.split(' / ', 2)]
        start, end = re.split(r'[~\-]', times, maxsplit=1)
    except ValueError:
        raise ValueError('사용법: !저장 이름 / YouTube주소 / 0:10~0:40') from None
    return validate_song({'name': name, 'url': url, 'start': start, 'end': end})
