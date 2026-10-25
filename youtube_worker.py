"""One-shot yt-dlp worker. Never imported by the long-running bot.

Only the few fields needed for playback cross the process boundary. Cookie
contents, signed stream URLs and extractor diagnostics never go into logs.
"""
from __future__ import annotations
import contextlib
import json
import sys


class QuietLogger:
    def debug(self, message):
        pass

    warning = debug
    error = debug


def extract(url: str, cookie_path: str | None = None) -> dict:
    import yt_dlp
    from validation import youtube_url

    options = {
        'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
        'socket_timeout': 15, 'noplaylist': True, 'retries': 1,
        'extractor_retries': 1, 'cachedir': False, 'logger': QuietLogger(),
    }
    if cookie_path:
        options['cookiefile'] = cookie_path
    with contextlib.redirect_stdout(sys.stderr):
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(youtube_url(url), download=False)
    if not isinstance(info, dict) or not info.get('url') or info.get('is_live'):
        raise ValueError('unsupported media')
    target = str(info['url'])
    if not target.startswith('https://') or len(target) > 32768:
        raise ValueError('invalid stream URL')
    # The existing player uses only User-Agent, not the whole extractor result.
    user_agent = str((info.get('http_headers') or {}).get('User-Agent', ''))
    return {
        'url': target,
        'duration': info.get('duration'),
        'http_headers': {'User-Agent': user_agent[:4096]},
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError('oversize request')
        request = json.loads(raw)
        info = extract(request['url'], request.get('cookie_path'))
        sys.stdout.write(json.dumps(info, ensure_ascii=True, allow_nan=False, separators=(',', ':')))
        return 0
    except Exception:
        # Parent supplies the useful, credential-free user error message.
        sys.stdout.write('{"error":"extraction_failed"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
