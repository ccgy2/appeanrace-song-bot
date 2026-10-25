"""Bounded, shared YouTube resolution without a permanently resident yt-dlp.

One short-lived child performs extraction; its memory (and Deno helpers) is
released on completion, cancellation or timeout. No audio is downloaded here.
"""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import parse_qs, urlsplit
from config import Settings
from validation import youtube_url


class YouTubeResolver:
    def __init__(self, settings: Settings, cookie_path: str | None = None):
        self.cookie_path = cookie_path
        self.ttl = settings.youtube_cache_seconds
        self.max_entries = settings.youtube_cache_entries
        self.timeout = 45.0
        self.slots = asyncio.Semaphore(settings.youtube_max_concurrent)
        self.cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.pending: dict[str, dict] = {}
        self.closed = False

    async def resolve(self, url: str) -> dict:
        if self.closed:
            raise ValueError('재생 준비가 종료 중입니다. 잠시 후 다시 시도하세요.')
        url = youtube_url(url)
        now = time.monotonic()
        for key, (deadline, _) in list(self.cache.items()):
            if deadline <= now:
                self.cache.pop(key, None)
        cached = self.cache.get(url)
        if cached:
            self.cache.move_to_end(url)
            return deepcopy(cached[1])
        entry = self.pending.get(url)
        if entry is None:
            if len(self.pending) >= 8:
                raise ValueError('재생 준비 요청이 많습니다. 잠시 후 다시 시도하세요.')
            task = asyncio.create_task(self._produce(url))
            entry = {'task': task, 'waiters': 0}
            self.pending[url] = entry

            def finished(done):
                if self.pending.get(url) is entry:
                    self.pending.pop(url, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        entry['waiters'] += 1
        try:
            return deepcopy(await asyncio.shield(entry['task']))
        finally:
            entry['waiters'] -= 1
            if not entry['waiters'] and not entry['task'].done():
                # A shared request survives until its last interested player
                # leaves. Cancel obsolete work instead of spending CPU on it.
                if self.pending.get(url) is entry:
                    self.pending.pop(url, None)
                entry['task'].cancel()
                await asyncio.gather(entry['task'], return_exceptions=True)

    async def _produce(self, url: str) -> dict:
        async with self.slots:
            info = await self._run_worker(url)
        if not isinstance(info, dict) or not isinstance(info.get('url'), str):
            raise ValueError('유튜브 오디오 주소를 가져오지 못했습니다.')
        if not info['url'].startswith('https://') or len(info['url']) > 32768:
            raise ValueError('안전한 오디오 주소가 아닙니다.')
        duration = info.get('duration')
        if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
            raise ValueError('유튜브 오디오 길이를 확인할 수 없습니다.')
        headers = info.get('http_headers') or {}
        if not isinstance(headers, dict):
            raise ValueError('유튜브 오디오 응답이 올바르지 않습니다.')
        info = {'url': info['url'], 'duration': duration,
                'http_headers': {'User-Agent': str(headers.get('User-Agent', ''))[:4096]}}
        ttl = float(self.ttl)
        # Signed playback URLs can expire earlier than our short cache TTL.
        try:
            expires = parse_qs(urlsplit(info['url']).query).get('expire', [])
            if expires:
                expiry = float(expires[0])
                ttl = min(ttl, expiry - time.time() - 60) if math.isfinite(expiry) else 0
        except (ValueError, TypeError, OverflowError):
            ttl = 0
        if ttl > 0 and self.max_entries and not self.closed:
            self.cache[url] = (time.monotonic() + ttl, deepcopy(info))
            self.cache.move_to_end(url)
            while len(self.cache) > self.max_entries:
                self.cache.popitem(last=False)
        return info

    async def _run_worker(self, url: str) -> dict:
        worker = Path(__file__).with_name('youtube_worker.py')
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-u', str(worker), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name == 'posix'),
        )
        try:
            request = json.dumps({'url': url, 'cookie_path': self.cookie_path}).encode()
            raw, _ = await asyncio.wait_for(process.communicate(request), timeout=self.timeout)
            if process.returncode != 0 or len(raw) > 65536:
                raise ValueError('YouTube 오디오를 가져오지 못했습니다.')
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError('YouTube 응답을 읽지 못했습니다.') from None
        finally:
            # Kill the entire process group so a timed-out Deno/JS helper
            # cannot remain alive and continue using billed resources.
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.kill()
            await process.wait()

    def invalidate(self, url: str):
        self.cache.pop(youtube_url(url), None)

    async def close(self):
        self.closed = True
        tasks = [entry['task'] for entry in self.pending.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.cache.clear()
