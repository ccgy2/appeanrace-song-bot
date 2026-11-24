"""Real local resumable-file tests; no cloud credentials or large allocations."""
import asyncio
import ast
from dataclasses import replace
import errno
import hashlib
import io
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from assets import Assets
from config import Settings
from upload_sessions import UploadSessions, CHUNK_BYTES, MAX_UPLOAD_BYTES, TTL_SECONDS, MAX_OWNER_SESSIONS
from validation import saved_volume_percent


def wav_bytes(payload_bytes=16000):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\x00\x00' * (payload_bytes // 2))
    return stream.getvalue()


async def chunks(data, size=65536):
    for start in range(0, len(data), size):
        yield data[start:start + size]


class UploadSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='appearance-chunk-test-')
        self.settings = Settings(data_dir=Path(self.tmp.name), max_upload_mb=10240, max_storage_mb=20480)
        self.assets = Assets(self.settings)
        self.sessions = UploadSessions(self.assets)
        self.addAsyncCleanup(self.sessions.close)
        self.addCleanup(self.tmp.cleanup)

    async def upload_data(self, data, filename='테스트.wav', owner='registrar'):
        current = await self.sessions.start(owner, filename, len(data))
        for offset in range(0, len(data), CHUNK_BYTES):
            current = await self.sessions.append(owner, current['id'], offset, chunks(data[offset:offset + CHUNK_BYTES]))
        return current

    async def completed(self, ident, owner='registrar'):
        current = await self.sessions.finish(owner, ident)
        for _ in range(200):
            if current['status'] in {'complete', 'error'}:
                return current
            await asyncio.sleep(.01)
            current = await self.sessions.status(owner, ident)
        self.fail('background upload processing did not complete')

    async def test_size_boundary_reserves_ten_gib_without_allocating_payload(self):
        roomy = SimpleNamespace(free=50 * 1024**3)
        with patch('assets.shutil.disk_usage', return_value=roomy):
            maximum = await self.sessions.start('registrar', '최대.mp4', MAX_UPLOAD_BYTES)
        self.assertEqual(maximum['size'], 10 * 1024**3)
        self.assertEqual(maximum['offset'], 0)
        self.assertEqual(self.sessions._part(maximum['id']).stat().st_size, 0)
        for size in (MAX_UPLOAD_BYTES + 1, 0, -1, True, 1.5, '100'):
            with self.subTest(size=size), self.assertRaises(ValueError):
                await self.sessions.start('registrar', '금지.mp4', size)

    async def test_smaller_configured_limit_is_respected(self):
        self.assets.settings = replace(self.settings, max_upload_mb=1)
        with self.assertRaises(ValueError):
            await self.sessions.start('registrar', '너무큰.wav', 1024 * 1024 + 1)

    async def test_lost_create_response_reuses_only_owner_and_explicit_request_id(self):
        request_id = 'a' * 32
        roomy = SimpleNamespace(free=50 * 1024**3)
        with patch('assets.shutil.disk_usage', return_value=roomy):
            original = await self.sessions.start('registrar', '대용량.mp4', MAX_UPLOAD_BYTES, request_id)
            after_lost_response = await self.sessions.start('registrar', '대용량.mp4', MAX_UPLOAD_BYTES, request_id)
        self.assertEqual(original, after_lost_response)
        self.assertEqual(len(list(self.sessions.root.glob('*.json'))), 1)
        self.assertEqual(self.sessions._read(original['id'])['requestId'], request_id)
        self.assertNotIn('requestId', original)
        await self.sessions.close()
        self.sessions = UploadSessions(self.assets)
        self.addAsyncCleanup(self.sessions.close)
        with patch('assets.shutil.disk_usage', return_value=roomy):
            self.assertEqual(await self.sessions.start('registrar', '대용량.mp4', MAX_UPLOAD_BYTES, request_id), original)
        for filename, size in [('다른.mp4', MAX_UPLOAD_BYTES), ('대용량.mp4', MAX_UPLOAD_BYTES - 1)]:
            with self.assertRaisesRegex(ValueError, '다른 파일'):
                await self.sessions.start('registrar', filename, size, request_id)
        # Isolate this test from a real machine's free disk without allocating it.
        await self.sessions.cancel('registrar', original['id'])
        first = await self.sessions.start('registrar', '같은.wav', 100, 'b' * 32)
        second = await self.sessions.start('other', '같은.wav', 100, 'b' * 32)
        third = await self.sessions.start('registrar', '같은.wav', 100, 'c' * 32)
        legacy = await self.sessions.start('registrar', '같은.wav', 100)
        self.assertEqual(len({first['id'], second['id'], third['id'], legacy['id']}), 4)
        self.assertNotIn('requestId', self.sessions._read(legacy['id']))
        for invalid in ('', 'A' * 32, '../file', 'g' * 32, 1, True):
            with self.assertRaises(ValueError):
                await self.sessions.start('other', '같은.wav', 100, invalid)

    async def test_create_retry_after_completion_returns_the_saved_asset(self):
        data, request_id = wav_bytes(), 'd' * 32
        started = await self.sessions.start('registrar', '완료.wav', len(data), request_id)
        await self.sessions.append('registrar', started['id'], 0, chunks(data))
        complete = await self.completed(started['id'])
        self.assertEqual(await self.sessions.start('registrar', '완료.wav', len(data), request_id), complete)

    async def test_multichunk_complete_has_same_bytes_hash_and_one_payload(self):
        data = wav_bytes(CHUNK_BYTES + 16000)
        staged = await self.upload_data(data)
        before_inode = self.sessions._part(staged['id']).stat().st_ino
        final = await self.completed(staged['id'])
        self.assertEqual(final['status'], 'complete', final)
        asset = self.assets.get(staged['id'])
        self.assertEqual(asset['path'].read_bytes(), data)
        self.assertEqual(asset['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(asset['path'].stat().st_ino, before_inode)  # Same-volume rename.
        self.assertFalse(self.sessions._part(staged['id']).exists())
        self.assertEqual(len(list(self.assets.root.glob('*.wav'))), 1)
        self.assertNotIn('path', final['result'])
        self.assertNotIn('storedName', final['result'])
        self.assertNotIn('owner', final)
        self.assertEqual(await self.sessions.finish('registrar', staged['id']), final)
        self.assertEqual((await self.sessions.cancel('registrar', staged['id']))['status'], 'complete')
        self.assertTrue(asset['path'].exists())

    async def test_response_loss_and_duplicate_chunk_do_not_append_twice(self):
        data = wav_bytes(CHUNK_BYTES + 100)
        staged = await self.sessions.start('registrar', '반복.wav', len(data))
        ident = staged['id']
        first = data[:CHUNK_BYTES]
        once = await self.sessions.append('registrar', ident, 0, chunks(first))
        twice = await self.sessions.append('registrar', ident, 0, chunks(first))
        self.assertEqual(once, twice)
        self.assertEqual(self.sessions._part(ident).stat().st_size, CHUNK_BYTES)
        with self.assertRaisesRegex(ValueError, '내용이 다릅니다'):
            await self.sessions.append('registrar', ident, 0, chunks(b'x' * CHUNK_BYTES))
        self.assertEqual(self.sessions._part(ident).read_bytes(), first)

    async def test_restart_truncates_uncommitted_tail_and_resumes_from_checkpoint(self):
        data = wav_bytes(CHUNK_BYTES + 200)
        staged = await self.sessions.start('registrar', '재시작.wav', len(data))
        ident = staged['id']
        await self.sessions.append('registrar', ident, 0, chunks(data[:CHUNK_BYTES]))
        with self.sessions._part(ident).open('ab') as stream:
            stream.write(b'uncommitted network tail')
        await self.sessions.close()
        self.sessions = UploadSessions(self.assets)
        self.addAsyncCleanup(self.sessions.close)
        current = await self.sessions.status('registrar', ident)
        self.assertEqual(current['offset'], CHUNK_BYTES)
        self.assertEqual(self.sessions._part(ident).stat().st_size, CHUNK_BYTES)
        await self.sessions.append('registrar', ident, CHUNK_BYTES, chunks(data[CHUNK_BYTES:]))
        self.assertEqual((await self.completed(ident))['status'], 'complete')

    async def test_truncated_chunk_wrong_offset_and_oversize_do_not_advance(self):
        data = wav_bytes()
        staged = await self.sessions.start('registrar', '불완전.wav', len(data))
        ident = staged['id']
        for offset, supplied in [(0, data[:-1]), (0, data + b'extra'), (1, data)]:
            with self.subTest(offset=offset, size=len(supplied)), self.assertRaises(ValueError):
                await self.sessions.append('registrar', ident, offset, chunks(supplied))
            self.assertEqual((await self.sessions.status('registrar', ident))['offset'], 0)
            self.assertEqual(self.sessions._part(ident).stat().st_size, 0)
        with self.assertRaises(ValueError):
            await self.sessions.finish('registrar', ident)
        await self.sessions.append('registrar', ident, 0, chunks(data))
        self.assertEqual((await self.completed(ident))['status'], 'complete')

    async def test_dropped_connection_and_cancelled_request_preserve_resume_position(self):
        data = wav_bytes()
        staged = await self.sessions.start('registrar', '끊김.wav', len(data))
        ident = staged['id']
        async def broken():
            yield data[:100]
            raise ConnectionResetError('test disconnect')
        with self.assertRaises(ValueError):
            await self.sessions.append('registrar', ident, 0, broken())
        waiting = asyncio.Event()
        async def interrupted():
            yield data[:100]
            waiting.set()
            await asyncio.Event().wait()
        request = asyncio.create_task(self.sessions.append('registrar', ident, 0, interrupted()))
        await waiting.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual((await self.sessions.status('registrar', ident))['offset'], 0)
        self.assertEqual(self.sessions._part(ident).stat().st_size, 0)

    async def test_cross_account_access_cannot_read_change_finish_or_cancel(self):
        data = wav_bytes()
        staged = await self.sessions.start('registrar', '개인.wav', len(data))
        ident = staged['id']
        for action in (
            lambda: self.sessions.status('other', ident),
            lambda: self.sessions.append('other', ident, 0, chunks(data)),
            lambda: self.sessions.finish('other', ident),
            lambda: self.sessions.cancel('other', ident),
        ):
            with self.assertRaises(PermissionError):
                await action()
        self.assertEqual((await self.sessions.status('registrar', ident))['offset'], 0)

    async def test_pending_reservations_apply_to_other_sessions_and_legacy_uploads(self):
        self.assets.settings = replace(self.settings, max_storage_mb=1)
        first = await self.sessions.start('registrar', '예약.wav', 900000)
        with self.assertRaisesRegex(ValueError, '저장 공간'):
            await self.sessions.start('other', '초과.wav', 200000)
        self.assertLess(self.assets.remaining_upload_bytes(), 200000)
        await self.sessions.cancel('registrar', first['id'])
        second = await self.sessions.start('other', '가능.wav', 200000)
        self.assertEqual(second['status'], 'uploading')

    async def test_active_session_limit_and_expiry_cleanup_release_reservations(self):
        existing = []
        for _ in range(MAX_OWNER_SESSIONS):
            existing.append(await self.sessions.start('registrar', '대기.wav', 100))
        with self.assertRaisesRegex(ValueError, '진행 중'):
            await self.sessions.start('registrar', '추가.wav', 100)
        expired = existing[0]['id']
        manifest = json.loads(self.sessions._manifest(expired).read_text())
        manifest['updatedAt'] = time.time() - TTL_SECONDS - 1
        self.sessions._write(manifest)
        await self.sessions.start('registrar', '정리후.wav', 100)
        self.assertFalse(self.sessions._manifest(expired).exists())
        self.assertFalse(self.sessions._part(expired).exists())
        with self.assertRaises(FileNotFoundError):
            await self.sessions.status('registrar', expired)

    async def test_enospc_during_chunk_keeps_checkpoint_and_allows_retry(self):
        data = wav_bytes()
        staged = await self.sessions.start('registrar', '공간.wav', len(data))
        ident = staged['id']
        original = self.sessions._piece
        def no_space(path, offset, block, compare=False):
            original(path, offset, block[:100], compare)
            raise OSError(errno.ENOSPC, 'secret/server/path')
        with patch.object(self.sessions, '_piece', side_effect=no_space):
            with self.assertRaisesRegex(ValueError, '저장 공간') as error:
                await self.sessions.append('registrar', ident, 0, chunks(data))
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual((await self.sessions.status('registrar', ident))['offset'], 0)
        self.assertEqual(self.sessions._part(ident).stat().st_size, 0)
        await self.sessions.append('registrar', ident, 0, chunks(data))
        self.assertEqual((await self.completed(ident))['status'], 'complete')

    async def test_checkpoint_failure_rolls_back_only_uncommitted_bytes(self):
        data = wav_bytes()
        staged = await self.sessions.start('registrar', '체크포인트.wav', len(data))
        ident = staged['id']
        with patch.object(self.sessions, '_write', side_effect=OSError(errno.ENOSPC, 'full')):
            with self.assertRaises(ValueError):
                await self.sessions.append('registrar', ident, 0, chunks(data))
        self.assertEqual(self.sessions._part(ident).stat().st_size, 0)
        original = self.sessions._write
        def committed_then_failed(item):
            original(item)
            raise OSError(errno.EIO, 'lost checkpoint acknowledgement')
        with patch.object(self.sessions, '_write', side_effect=committed_then_failed):
            with self.assertRaises(ValueError):
                await self.sessions.append('registrar', ident, 0, chunks(data))
        self.assertEqual((await self.sessions.status('registrar', ident))['offset'], len(data))
        self.assertEqual(self.sessions._part(ident).read_bytes(), data)

    async def test_background_probe_is_off_loop_and_status_does_not_wait(self):
        staged = await self.upload_data(wav_bytes())
        started, release = threading.Event(), threading.Event()
        original = self.assets.inspect_staged
        loop_thread = threading.get_ident()
        observed = []
        def blocked(path, size, stop):
            observed.append(threading.get_ident())
            started.set()
            release.wait(2)
            return original(path, size, stop)
        with patch.object(self.assets, 'inspect_staged', side_effect=blocked):
            self.assertEqual((await self.sessions.finish('registrar', staged['id']))['status'], 'processing')
            await asyncio.to_thread(started.wait, 1)
            current = await asyncio.wait_for(self.sessions.status('registrar', staged['id']), .5)
            self.assertEqual(current['status'], 'processing')
            self.assertNotEqual(observed[0], loop_thread)
            release.set()
            self.assertEqual((await self.completed(staged['id']))['status'], 'complete')

    async def test_shutdown_during_processing_preserves_file_for_restart(self):
        staged = await self.upload_data(wav_bytes())
        ident = staged['id']
        started = threading.Event()
        def interrupted_by_shutdown(path, size, stop):
            started.set()
            stop.wait(2)
            raise InterruptedError('graceful shutdown')
        with patch.object(self.assets, 'inspect_staged', side_effect=interrupted_by_shutdown):
            await self.sessions.finish('registrar', ident)
            await asyncio.to_thread(started.wait, 1)
            await self.sessions.close()
        self.assertEqual(self.sessions._read(ident)['status'], 'processing')
        self.assertTrue(self.sessions._part(ident).exists())
        self.sessions = UploadSessions(self.assets)
        self.addAsyncCleanup(self.sessions.close)
        self.assertEqual((await self.completed(ident))['status'], 'complete')

    async def test_cancel_during_processing_removes_only_unpublished_payload(self):
        staged = await self.upload_data(wav_bytes())
        ident = staged['id']
        started = threading.Event()
        def interrupted_by_cancel(path, size, stop):
            started.set()
            stop.wait(2)
            raise InterruptedError('cancelled')
        with patch.object(self.assets, 'inspect_staged', side_effect=interrupted_by_cancel):
            await self.sessions.finish('registrar', ident)
            await asyncio.to_thread(started.wait, 1)
            result = await self.sessions.cancel('registrar', ident)
        self.assertEqual(result['status'], 'error')
        self.assertIn('취소', result['error'])
        self.assertFalse(self.sessions._part(ident).exists())
        self.assertEqual(list(self.assets.root.iterdir()), [])
        self.assertEqual((await self.sessions.status('registrar', ident))['status'], 'error')

    async def test_final_metadata_storage_failure_never_reports_success(self):
        staged = await self.upload_data(wav_bytes())
        ident = staged['id']
        def fail_after_rename(asset_id, name, stage, size, inspected):
            stage.rename(self.assets.root / (asset_id + '.wav'))
            raise OSError(errno.ENOSPC, 'private/path')
        with patch.object(self.assets, 'commit_staged', side_effect=fail_after_rename):
            result = await self.completed(ident)
        self.assertEqual(result['status'], 'error')
        self.assertIn('저장 공간', result['error'])
        self.assertNotIn('private', result['error'])
        self.assertEqual(list(self.assets.root.iterdir()), [])
        self.assertFalse(self.sessions._part(ident).exists())

    async def test_parallel_duplicate_requests_commit_exactly_once(self):
        data = wav_bytes()
        staged = await self.sessions.start('registrar', '동시.wav', len(data))
        ident = staged['id']
        results = await asyncio.gather(
            self.sessions.append('registrar', ident, 0, chunks(data)),
            self.sessions.append('registrar', ident, 0, chunks(data)),
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.sessions._part(ident).read_bytes(), data)

    async def test_restart_after_rename_before_metadata_does_not_copy_payload(self):
        data = wav_bytes()
        staged = await self.upload_data(data)
        ident = staged['id']
        item = self.sessions._read(ident)
        item['status'] = 'processing'
        self.sessions._write(item)
        target = self.assets.root / (ident + '.wav')
        self.sessions._part(ident).rename(target)
        inode = target.stat().st_ino
        await self.sessions.close()
        self.sessions = UploadSessions(self.assets)
        self.addAsyncCleanup(self.sessions.close)
        self.assertEqual((await self.sessions.status('registrar', ident))['status'], 'processing')
        self.assertEqual((await self.completed(ident))['status'], 'complete')
        self.assertEqual(self.assets.get(ident)['path'].stat().st_ino, inode)

    async def test_completed_asset_survives_expired_session_and_response_loss(self):
        staged = await self.upload_data(wav_bytes())
        ident = staged['id']
        await self.completed(ident)
        asset = self.assets.get(ident)
        item = self.sessions._read(ident)
        item['status'] = 'processing'
        item.pop('result')
        self.sessions._write(item)
        self.assertEqual((await self.sessions.status('registrar', ident))['status'], 'complete')
        item = self.sessions._read(ident)
        item['updatedAt'] = time.time() - TTL_SECONDS - 1
        self.sessions._write(item)
        await self.sessions.start('registrar', '다음.wav', 100)
        self.assertTrue(asset['path'].exists())
        self.assertFalse(self.sessions._manifest(ident).exists())

    async def test_invalid_audio_cleans_payload_but_keeps_readable_error(self):
        staged = await self.upload_data(b'not real audio', '잘못된.mp4')
        finished = await self.completed(staged['id'])
        self.assertEqual(finished['status'], 'error')
        self.assertIn('오디오', finished['error'])
        self.assertFalse(self.sessions._part(staged['id']).exists())
        self.assertEqual(list(self.assets.root.iterdir()), [])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required for real MP4 fixtures')
    async def test_real_mp4_with_audio_accepted_and_video_only_rejected(self):
        for with_audio in (True, False):
            target = Path(self.tmp.name) / ('sound.mp4' if with_audio else 'silent-video.mp4')
            command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:r=5']
            if with_audio:
                command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=8000']
            command += ['-t', '0.4', '-c:v', 'mpeg4']
            if with_audio:
                command += ['-c:a', 'aac', '-shortest']
            command += ['-y', str(target)]
            await asyncio.to_thread(subprocess.run, command, check=True, capture_output=True, timeout=15)
            data = target.read_bytes()
            staged = await self.upload_data(data, target.name)
            result = await self.completed(staged['id'])
            self.assertEqual(result['status'], 'complete' if with_audio else 'error', result)
            if with_audio:
                self.assertEqual(self.assets.get(staged['id'])['path'].suffix, '.mp4')
                self.assertEqual(self.assets.get(staged['id'])['path'].read_bytes(), data)
                # Execute the actual source-option construction with only the
                # Discord wrapper substituted; no SDK installation/network is
                # required. Decode the real uploaded AV container using those
                # captured options and Discord's PCM output format.
                player_path = Path(__file__).resolve().parents[1] / 'player.py'
                tree = ast.parse(player_path.read_text(encoding='utf-8'))
                player = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Player')
                method = next(node for node in player.body if isinstance(node, ast.FunctionDef) and node.name == '_source')
                captured = {}
                def decoder(source, **kwargs):
                    captured.update(source=source, **kwargs)
                    return SimpleNamespace(_process=None)
                namespace = {
                    'saved_volume_percent': saved_volume_percent, 'subprocess': subprocess,
                    'discord': SimpleNamespace(FFmpegPCMAudio=decoder,
                                               PCMVolumeTransformer=lambda value, volume: SimpleNamespace(volume=volume)),
                }
                exec(compile(ast.Module(body=[method], type_ignores=[]), str(player_path), 'exec'), namespace)
                namespace['_source'](SimpleNamespace(settings=self.settings), self.assets.get(staged['id'])['path'],
                                     before='-nostdin -protocol_whitelist file,pipe', options='-vn -t 0.200',
                                     master=.5, record={'volumePercent': 100}, team='테스트')
                actual_options = shlex.split(captured['options'])
                self.assertIn('-vn', actual_options)
                decoded = await asyncio.to_thread(
                    subprocess.run,
                    [captured['executable'], '-hide_banner', '-loglevel', 'error',
                     *shlex.split(captured['before_options']), '-i', captured['source'],
                     '-f', 's16le', '-ar', '48000', '-ac', '2', *actual_options, 'pipe:1'],
                    check=True, capture_output=True, timeout=15,
                )
                self.assertEqual(len(decoded.stdout), 48000 * 2 * 2 // 5)  # 200ms of stereo signed16 PCM.
                self.assertTrue(any(decoded.stdout))  # The sine audio was decoded, not the video or silence.
            else:
                self.assertIn('오디오', result['error'])


if __name__ == '__main__':
    unittest.main()
