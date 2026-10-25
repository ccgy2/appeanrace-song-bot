"""Public discord.py encoder profiles; PCM volume behavior stays unchanged.

The economy profile trades packet-loss recovery for less encoding work and
lower voice bitrate. It never patches libopus state or Discord internals.
"""
from __future__ import annotations


def validate_audio_profile(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('AUDIO_PROFILE은 standard 또는 economy여야 합니다.')
    profile = value.strip().lower()
    if profile not in {'standard', 'economy'}:
        raise ValueError('AUDIO_PROFILE은 standard 또는 economy여야 합니다.')
    return profile


def voice_play_options(profile: str) -> dict:
    """Return a fresh kwargs dict accepted by VoiceClient.play in discord.py 2.7.

    Empty standard options deliberately preserve the previous call and pinned
    library defaults: 128 kbps, FEC enabled, full bandwidth and auto signal.
    """
    if validate_audio_profile(profile) == 'standard':
        return {}
    return {
        'application': 'audio',
        'bitrate': 96,
        'fec': False,
        'bandwidth': 'full',
        'signal_type': 'music',
    }


def audio_profile_info(profile: str) -> dict:
    profile = validate_audio_profile(profile)
    economy = profile == 'economy'
    return {
        'id': profile,
        'label': '절약형 음악 재생' if economy else '기존 음성 설정',
        'bitrateKbps': 96 if economy else 128,
        'forwardErrorCorrection': not economy,
        'note': ('전송량과 인코딩 작업을 줄입니다. 패킷 손실 복구를 끄므로 '
                 '불안정한 네트워크에서는 소리 끊김이 늘 수 있습니다. '
                 'AUDIO_PROFILE=standard로 기존 설정을 복원할 수 있습니다.'
                 if economy else '기존 128kbps와 패킷 손실 복구 설정을 유지합니다.'),
    }
