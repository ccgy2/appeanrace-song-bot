"""Offline encoder comparison; no Discord connection or YouTube request.

Run: python tests/benchmark_audio_profiles.py --repeats 5
CPU seconds measure just the encoder; these are not Railway bill forecasts.
"""
from __future__ import annotations
import argparse
import audioop
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import discord.opus
from audio_runtime import voice_play_options


def benchmark(source: Path, repeats: int = 5, seconds: int = 30) -> dict:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError('A local audio file is required.')
    decode_samples = []
    for _ in range(repeats):
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = subprocess.run([
            'ffmpeg', '-nostdin', '-threads', '1', '-i', str(source),
            '-f', 's16le', '-ar', '48000', '-ac', '2', '-loglevel', 'error',
            '-t', str(seconds), '-threads', '1', '-filter_threads', '1', 'pipe:1',
        ], capture_output=True, check=True, timeout=60)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        decode_samples.append(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)
    # The app's untouched initial master gain is 50% with song gain 100%.
    pcm = audioop.mul(result.stdout, 2, .5)
    frames = [pcm[i:i + 3840] for i in range(0, len(pcm) - 3839, 3840)]
    profiles = {}
    for profile in ('standard', 'economy'):
        runs = []
        for _ in range(repeats):
            encoder = discord.opus.Encoder(**voice_play_options(profile))
            start = time.process_time()
            packets = [encoder.encode(frame, 960) for frame in frames]
            cpu_seconds = time.process_time() - start
            decoder = discord.opus.Decoder()
            decoded_frames = 0
            for packet in packets:
                decoded = decoder.decode(packet, fec=False)
                if len(decoded) != 3840:
                    raise AssertionError('Opus packet did not decode to one complete stereo PCM frame.')
                decoded_frames += 1
            runs.append({
                'encoder_cpu_seconds': round(cpu_seconds, 6),
                'packet_payload_bytes': sum(map(len, packets)),
                'decoded_frames': decoded_frames,
            })
        profiles[profile] = {
            'encoder_cpu_seconds_median': round(statistics.median(r['encoder_cpu_seconds'] for r in runs), 6),
            'packet_payload_bytes_median': statistics.median(r['packet_payload_bytes'] for r in runs),
            'runs': runs,
        }
    return {
        'source': str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else source.name,
        'python': sys.version.split()[0],
        'discord_py': discord.__version__,
        'opus': discord.opus.Encoder.get_opus_version(),
        'gain': .5,
        'audio_seconds': len(frames) * .02,
        'frames_per_run': len(frames),
        'repeats': repeats,
        'decode_cpu_seconds_median': round(statistics.median(decode_samples), 6),
        'decode_cpu_seconds_samples': decode_samples,
        'profiles': profiles,
        'scope': 'Local decoding and native Opus only; no Discord network, encryption, bot DB or billing measurement. Packet bytes exclude transport overhead.',
        'tradeoff': 'Economy uses 96 kbps and disables Opus in-band FEC; packet loss may be more audible. Both profiles retain dynamic PCM volume and full audio bandwidth.',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'sounds' / 'homerun.mp3')
    parser.add_argument('--repeats', type=int, choices=range(1, 11), default=5)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.source, repeats=args.repeats), ensure_ascii=False, indent=2))
