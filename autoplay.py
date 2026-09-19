"""Discord 통화방 자동 등장곡 선택 규칙. Discord 라이브러리 없이 테스트 가능."""
from __future__ import annotations


def select_auto_song(songs: list[dict], lineup: dict[str, str], member_id: str,
                     display_name: str, has_legacy_role: bool) -> tuple[dict | None, int | None]:
    """자동 재생할 곡과 타순을 고른다.

    - memberId가 명시된 곡은 ID 일치만으로 자동 재생 대상이다.
      역할/타순/닉네임은 요구하지 않는다.
    - memberId가 없는 기존 데이터는 예전 규칙(등장곡 재생인 역할 + 타순 + 닉네임)을 유지한다.
    """
    member_id = str(member_id or '')
    song = next((s for s in songs if str(s.get('memberId') or '') == member_id and member_id), None)
    if song:
        order = next((int(o) for o, name in lineup.items() if name == song.get('name')), None)
        return song, order

    if not has_legacy_role:
        return None, None

    for o, name in lineup.items():
        candidate = next((s for s in songs if s.get('name') == name and not s.get('memberId')), None)
        if candidate and name == display_name:
            return candidate, int(o)
    return None, None
