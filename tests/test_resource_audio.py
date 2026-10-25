"""Resource regressions: real local workers/FFmpeg, no Discord or YouTube network."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from assets import Assets
from config import Settings
from player import Player
from storage import Store
from youtube_resolver import YouTubeResolver

URL = 'https://www.youtube.com/watch?v=abcdefghijk'
URL2 = 'https://www.youtube.com/watch?v=12345678901'
URL3 = 'https://www.youtube.com/watch?v=zyxwvutsrqp'
INFO = {'url': 'https://audio.example.test/stream', 'duration': 60,
        'http_headers': {'User-Agent': 'test-agent'}}


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resolver = YouTubeResolver(Settings())
        self.resolver._run_worker = AsyncMock(return_value=INFO)

    async def asyncTearDown(self):
        await self.resolver.close()

    async def test_repeat_and_equivalent_links_share_small_cache(self):
        first = await self.resolver.resolve(URL)
        first['http_headers']['User-Agent'] = 'client mutation'
        second = await self.resolver.resolve('https://youtu.be/abcdefghijk?t=5')
        self.assertEqual(second, INFO)
        self.assertEqual(self.resolver._run_worker.await_count, 1)

    async def test_concurrent_requests_extract_once(self):
        values = await asyncio.gather(*(self.resolver.resolve(URL) for _ in range(12)))
        self.assertTrue(all(value == INFO for value in values))
        self.assertEqual(self.resolver._run_worker.await_count, 1)

    async def test_expired_cache_extracts_again(self):
        await self.resolver.resolve(URL)
        self.resolver.cache[URL] = (time.monotonic() - 1, INFO)
        await self.resolver.resolve(URL)
        self.assertEqual(self.resolver._run_worker.await_count, 2)

    async def test_signed_url_near_expiry_is_never_reused(self):
        self.resolver._run_worker.return_value = {**INFO, 'url': INFO['url'] + '?expire=' + str(int(time.time()) + 20)}
        await self.resolver.resolve(URL)
        await self.resolver.resolve(URL)
        self.assertEqual(self.resolver._run_worker.await_count, 2)

    async def test_lru_bounded_and_zero_disables_cache(self):
        self.resolver.max_entries = 2
        for url in (URL, URL2, URL, URL3):
            await self.resolver.resolve(url)
        self.assertEqual(list(self.resolver.cache), [URL, URL3])
        await self.resolver.resolve(URL2)
        self.assertEqual(self.resolver._run_worker.await_count, 4)
        self.resolver.max_entries = 0
        self.resolver.cache.clear()
        await self.resolver.resolve(URL)
        await self.resolver.resolve(URL)
        self.assertEqual(self.resolver._run_worker.await_count, 6)

    async def test_failure_not_cached_and_explicit_invalidation(self):
        self.resolver._run_worker.side_effect = [ValueError('fixture error'), INFO, INFO]
        with self.assertRaises(ValueError):
            await self.resolver.resolve(URL)
        await self.resolver.resolve(URL)
        self.resolver.invalidate(URL)
        await self.resolver.resolve(URL)
        self.assertEqual(self.resolver._run_worker.await_count, 3)

    async def test_cancel_one_listener_keeps_other_listener(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def extract(url):
            started.set()
            await release.wait()
            return INFO
        self.resolver._run_worker.side_effect = extract
        first = asyncio.create_task(self.resolver.resolve(URL))
        second = asyncio.create_task(self.resolver.resolve(URL))
        await started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await second, INFO)
        self.assertEqual(self.resolver._run_worker.await_count, 1)

    async def test_last_listener_cancellation_stops_work(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def extract(url):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.resolver._run_worker.side_effect = extract
        task = asyncio.create_task(self.resolver.resolve(URL))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.resolver.pending)

    async def test_different_urls_respect_one_worker_limit(self):
        active = peak = 0
        async def extract(url):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
            return INFO
        self.resolver._run_worker.side_effect = extract
        await asyncio.gather(*(self.resolver.resolve(url) for url in (URL, URL2, URL3)))
        self.assertEqual(peak, 1)

    async def test_unsafe_input_and_unsafe_worker_result_rejected(self):
        with self.assertRaises(ValueError):
            await self.resolver.resolve('http://127.0.0.1/private')
        self.resolver._run_worker.assert_not_called()
        self.resolver._run_worker.return_value = {**INFO, 'url': 'file:///etc/passwd'}
        with self.assertRaises(ValueError):
            await self.resolver.resolve(URL)
        self.assertFalse(self.resolver.cache)

    async def test_closed_resolver_rejects_new_work(self):
        await self.resolver.close()
        with self.assertRaises(ValueError):
            await self.resolver.resolve(URL)
        self.resolver._run_worker.assert_not_called()


class WorkerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, code, *, cancel=False):
        resolver = YouTubeResolver(Settings())
        resolver.timeout = .3
        spawned = []
        original = asyncio.create_subprocess_exec
        async def launch(*args, **kwargs):
            process = await original(sys.executable, '-u', '-c', code, **kwargs)
            spawned.append(process)
            return process
        try:
            with patch('youtube_resolver.asyncio.create_subprocess_exec', side_effect=launch):
                task = asyncio.create_task(resolver._run_worker(URL))
                if cancel:
                    while not spawned:
                        await asyncio.sleep(.005)
                    task.cancel()
                return await task
        finally:
            await resolver.close()
            self.assertTrue(spawned)
            self.assertIsNotNone(spawned[0].returncode, 'worker survived cleanup')

    async def test_real_process_success_exits(self):
        code = 'import sys,json; request=json.load(sys.stdin); print(' + repr(json.dumps(INFO)) + ')'
        self.assertEqual(await self.run_fixture(code), INFO)

    async def test_real_process_timeout_kills_worker(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.run_fixture('import time; time.sleep(30)')

    async def test_real_process_cancellation_kills_worker(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_fixture('import time; time.sleep(30)', cancel=True)

    async def test_real_process_error_and_invalid_json_fail_cleanly(self):
        for code in ['raise SystemExit(1)', 'print("not-json")']:
            with self.subTest(code=code), self.assertRaises(ValueError):
                await self.run_fixture(code)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process-group cleanup')
    async def test_timeout_also_stops_descendant_process(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'child.pid'
            code = ('import subprocess,sys,time,pathlib; '
                    'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
                    f'pathlib.Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(30)')
            with self.assertRaises(asyncio.TimeoutError):
                await self.run_fixture(code)
            pid = int(marker.read_text())
            status = Path('/proc') / str(pid) / 'stat'
            if status.exists():
                self.assertEqual(status.read_text().split(') ', 1)[1][0], 'Z')


class PlayerResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store = Store(self.settings)
        self.player = Player(self.settings, self.store, Assets(self.settings))
        self.guild = SimpleNamespace(id=1, voice_client=None, me=SimpleNamespace(voice=None))

    async def asyncTearDown(self):
        await self.player.close()
        await self.store.close()
        self.temp.cleanup()

    async def test_stop_cancels_obsolete_preparation_without_playing(self):
        started = asyncio.Event()
        async def prepare(url):
            started.set()
            await asyncio.Event().wait()
        self.player._extract_limited = AsyncMock(side_effect=prepare)
        self.player.voice.connect = AsyncMock()
        task = asyncio.create_task(self.player.play(self.guild, 'A팀', {'name': '테스트', 'url': URL}))
        await started.wait()
        self.player.stop(self.guild)
        self.assertFalse(await task)
        self.player.voice.connect.assert_not_called()
        self.assertEqual(self.player.now[1]['status'], 'stopped')

    async def test_next_play_cancels_old_preparation(self):
        started = asyncio.Event()
        async def prepare(url):
            if url == URL:
                started.set()
                await asyncio.Event().wait()
            return INFO
        self.player._extract_limited = AsyncMock(side_effect=prepare)
        self.player.voice.connect = AsyncMock(return_value=object())
        self.player._source = Mock(return_value=SimpleNamespace())
        self.player._start = Mock()
        first = asyncio.create_task(self.player.play(self.guild, 'A팀', {'name': 'old', 'url': URL}))
        await started.wait()
        self.assertTrue(await self.player.play(self.guild, 'A팀', {'name': 'new', 'url': URL2}))
        self.assertFalse(await first)
        self.player._start.assert_called_once()
        self.assertEqual(self.player._start.call_args.args[4], 'new')

    async def test_ffmpeg_thread_limit_keeps_pcm_and_volume(self):
        path = Path(self.temp.name) / 'test.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\x10\x00' * 8000)
        source = self.player._source(path, before='-nostdin', options='-vn -t 0.2',
                                     master=.5, record={'volumePercent': 160}, team='A팀')
        try:
            self.assertEqual(len(source.read()), 3840)
            self.assertAlmostEqual(source.volume, .8)
            self.assertFalse(source.is_opus())
            source.volume = .4
            self.assertEqual(len(source.read()), 3840)
        finally:
            source.cleanup()

    async def test_dual_bots_share_resolver_and_keep_voice_cache(self):
        from main import create_bot
        settings = replace(self.settings, web_enabled=False, web_only=True)
        first = create_bot(settings)
        second = create_bot(settings, slot='secondary', web_owner=False,
                            youtube_resolver=first.player.youtube)
        try:
            self.assertIs(first.player.youtube, second.player.youtube)
            self.assertIsNone(first._connection._messages)
            self.assertTrue(first._connection.member_cache_flags.voice)
            self.assertFalse(first.intents.typing)
            self.assertFalse(first.intents.reactions)
            self.assertTrue(first.intents.message_content)
            self.assertTrue(first.intents.voice_states)
            self.assertTrue(first.intents.messages)
        finally:
            await asyncio.gather(first.close(), second.close())

    async def test_hidden_ffmpeg_error_or_empty_stream_invalidates_url(self):
        self.player.youtube.invalidate = Mock()
        for internal_error, frames in [(RuntimeError('HTTP fixture failure'), 1), (None, 0)]:
            source = SimpleNamespace(
                original=SimpleNamespace(_current_error=internal_error, appearance_frame_count=frames),
                appearance_youtube_url=URL, appearance_song_percent=100, appearance_master_volume=1,
                cleanup=Mock(),
            )
            vc = SimpleNamespace(is_playing=lambda: False, is_paused=lambda: False, play=Mock())
            self.player._start(self.guild, vc, source, self.player.generation[1], '테스트', 'A팀')
            vc.play.call_args.kwargs['after'](None)
            await asyncio.sleep(0)
            self.assertEqual(self.player.now[1]['status'], 'error')
        self.assertEqual(self.player.youtube.invalidate.call_count, 2)


if __name__ == '__main__':
    unittest.main()
