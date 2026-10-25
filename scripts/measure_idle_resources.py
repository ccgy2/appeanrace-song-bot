"""No-login startup RSS comparison. Run each source tree in a fresh process.

python scripts/measure_idle_resources.py --source . --bots 1
This is a Linux development measurement, not a Railway bill forecast.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile


async def measure(source: Path, count: int):
    sys.path.insert(0, str(source.resolve()))
    from config import Settings
    from main import create_bot

    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(data_dir=Path(temp), web_only=True, web_enabled=False)
        bots = [create_bot(settings)]
        if count == 2:
            bots.append(create_bot(settings, slot='secondary', web_owner=False))
        try:
            await asyncio.sleep(.02)
            rss = None
            status = Path('/proc/self/status')
            if status.exists():
                for line in status.read_text().splitlines():
                    if line.startswith('VmRSS:'):
                        rss = int(line.split()[1]) * 1024
            print(json.dumps({'bots': count, 'rss_bytes': rss,
                              'rss_mib': round(rss / 1048576, 2) if rss else None,
                              'yt_dlp_loaded': 'yt_dlp' in sys.modules,
                              'scope': 'no-login Linux startup; no live traffic/audio'}, ensure_ascii=False))
        finally:
            await asyncio.gather(*(bot.close() for bot in bots))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--bots', type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    asyncio.run(measure(args.source, args.bots))
