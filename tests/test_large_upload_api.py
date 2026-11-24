"""Local HTTP regressions for resumable uploads and scoped streaming media.

Uses temporary SQLite/files, real short FFmpeg-generated MP4 fixtures and an
aiohttp TestServer. No Discord, Railway, Firebase or external network requests.
The ten-GiB boundary is checked through declared sizes, never a ten-GiB fixture.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from assets import Assets
from config import Settings
from storage import Store
from webapp import WebPanel


PASSWORD = 'large-upload-local-test-12345'
TEAM = '대용량 파일 테스트팀'
CHUNK_SIZE = 4 * 1024 * 1024
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024


class LargeUploadSettingsTests(unittest.TestCase):
    def test_default_ten_gib_cap_and_explicit_lower_limits_are_preserved(self):
        self.assertEqual(Settings().max_upload_mb * 1024 * 1024, MAX_FILE_SIZE)
        self.assertGreaterEqual(Settings().max_storage_mb, Settings().max_upload_mb)
        with tempfile.TemporaryDirectory(prefix='appearance-upload-config-') as folder:
            environ = {'DATA_DIR': folder, 'WEB_ADMIN_PASSWORD': PASSWORD, 'WEB_ONLY': 'true'}
            with patch.dict(os.environ, environ, clear=True):
                self.assertEqual(Settings.from_env().max_upload_mb, 10240)
            with patch.dict(os.environ, {**environ, 'MAX_UPLOAD_MB': '32', 'MAX_STORAGE_MB': '256'}, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.max_upload_mb, 32)
                self.assertEqual(settings.max_storage_mb, 256)
            with patch.dict(os.environ, {**environ, 'MAX_UPLOAD_MB': '999999'}, clear=True):
                self.assertEqual(Settings.from_env().max_upload_mb, 10240)


class LargeUploadAPI(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise unittest.SkipTest('These integration tests require local ffmpeg and ffprobe.')
        cls.fixtures = tempfile.TemporaryDirectory(prefix='appearance-mp4-fixtures-')
        folder = Path(cls.fixtures.name)
        common = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                  '-f', 'lavfi', '-i', 'color=c=black:s=32x32:r=8']
        subprocess.run(common + [
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '0.5', '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'mpeg4',
            '-q:v', '8', '-c:a', 'aac', '-movflags', '+faststart', str(folder / 'with-audio.mp4'),
        ], check=True, capture_output=True, timeout=20)
        subprocess.run(common + [
            '-t', '0.5', '-an', '-c:v', 'mpeg4', '-q:v', '8',
            '-movflags', '+faststart', str(folder / 'silent.mp4'),
        ], check=True, capture_output=True, timeout=20)
        cls.audio_mp4 = (folder / 'with-audio.mp4').read_bytes()
        cls.silent_mp4 = (folder / 'silent.mp4').read_bytes()
        # An ISO BMFF 'free' box keeps the real MP4 valid while crossing the
        # chunk boundary. No excessive-duration/audio fixture is necessary.
        padding_size = CHUNK_SIZE + 357 - len(cls.audio_mp4)
        cls.multichunk_mp4 = cls.audio_mp4 + struct.pack('>I4s', padding_size, b'free') + bytes(padding_size - 8)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='appearance-upload-api-')
        self.settings = Settings(data_dir=Path(self.tmp.name), web_password=PASSWORD, web_only=True,
                                 max_upload_mb=10240, max_storage_mb=20480)
        self.store = Store(self.settings)
        self.assets = Assets(self.settings)
        bot = SimpleNamespace(settings=self.settings, store=self.store, assets=self.assets,
                              guilds=[], is_ready=lambda: False, get_guild=lambda _: None,
                              player=SimpleNamespace(now={}))
        self.panel = WebPanel(bot)
        self.client = TestClient(TestServer(self.panel.app), cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()
        self.admin = await self.login('admin')
        await self.store.create_team(TEAM)

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.close()
        self.tmp.cleanup()

    async def login(self, username):
        result = await self.client.post('/api/login', json={
            'username': username, 'password': PASSWORD, 'authMode': 'bearer',
        })
        self.assertEqual(result.status, 200, await result.text())
        data = await result.json()
        return {'Authorization': 'Bearer ' + data['accessToken'], 'X-CSRF-Token': data['csrf']}

    async def account(self, role, suffix=''):
        username = 'upload_' + role + suffix
        result = await self.client.post('/api/signup', json={
            'username': username, 'displayName': role + suffix, 'password': PASSWORD,
        })
        self.assertEqual(result.status, 201, await result.text())
        result = await self.client.put('/api/users/' + username, headers=self.admin,
                                       json={'role': role, 'enabled': True})
        self.assertEqual(result.status, 200, await result.text())
        return await self.login(username)

    async def start(self, headers, size, filename='테스트.MP4'):
        result = await self.client.post('/api/uploads', headers=headers, json={'filename': filename, 'size': size})
        self.assertEqual(result.status, 201, await result.text())
        state = await result.json()
        self.assertRegex(state['id'], r'^[a-f0-9]{32}$')
        self.assertEqual(state['status'], 'uploading')
        self.assertEqual(state['offset'], 0)
        self.assertEqual(state['size'], size)
        self.assertEqual(state['chunkSize'], CHUNK_SIZE)
        return state

    async def status(self, headers, upload_id):
        result = await self.client.get('/api/uploads/' + upload_id, headers=headers)
        self.assertEqual(result.status, 200, await result.text())
        return await result.json()

    async def append(self, headers, upload_id, offset, data):
        if isinstance(data, (bytes, bytearray)):
            data = io.BytesIO(data)
        return await self.client.put('/api/uploads/' + upload_id, params={'offset': str(offset)},
                                     headers={**headers, 'Content-Type': 'application/octet-stream'}, data=data)

    async def finish(self, headers, upload_id):
        result = await self.client.post('/api/uploads/' + upload_id + '/complete', headers=headers, json={})
        self.assertIn(result.status, (200, 202), await result.text())
        state = await result.json()
        async with asyncio.timeout(10):
            while state['status'] in ('uploading', 'processing'):
                await asyncio.sleep(.02)
                state = await self.status(headers, upload_id)
        return state

    async def upload(self, headers, data=None, filename='테스트.MP4'):
        data = self.audio_mp4 if data is None else data
        state = await self.start(headers, len(data), filename)
        for offset in range(0, len(data), CHUNK_SIZE):
            chunk = data[offset:offset + CHUNK_SIZE]
            result = await self.append(headers, state['id'], offset, chunk)
            self.assertEqual(result.status, 200, await result.text())
            self.assertEqual((await result.json())['offset'], offset + len(chunk))
        complete = await self.finish(headers, state['id'])
        self.assertEqual(complete['status'], 'complete', complete)
        self.assertEqual(complete['offset'], len(data))
        return complete

    async def ticket(self, headers, asset_id, download=False):
        result = await self.client.post('/api/media/' + asset_id + '/ticket', headers=headers, json={'download': download})
        self.assertEqual(result.status, 200, await result.text())
        ticket = await result.json()
        self.assertEqual(urlsplit(ticket['url']).scheme, '')
        self.assertEqual(urlsplit(ticket['url']).netloc, '')
        self.assertEqual(urlsplit(ticket['url']).path, '/api/media/' + asset_id)
        self.assertGreater(ticket['expiresIn'], 0)
        self.assertLessEqual(ticket['expiresIn'], 3600)
        self.assertNotIn(headers['Authorization'].split(' ', 1)[1], ticket['url'])
        return ticket

    async def assert_error(self, result, status):
        self.assertEqual(result.status, status, await result.text())
        self.assertTrue((await result.json()).get('error'))

    async def test_admin_and_registrar_register_real_mp4_without_changing_its_bytes(self):
        for role in ('admin', 'registrar'):
            headers = self.admin if role == 'admin' else await self.account(role)
            with self.subTest(role=role):
                complete = await self.upload(headers)
                meta = complete['result']
                self.assertEqual(meta['bytes'], len(self.audio_mp4))
                self.assertEqual(meta['sha256'], hashlib.sha256(self.audio_mp4).hexdigest())
                self.assertGreater(meta['duration'], 0)
                stored = self.assets.get(meta['id'])
                self.assertEqual(stored['path'].suffix, '.mp4')
                self.assertEqual(stored['path'].read_bytes(), self.audio_mp4)
                result = await self.client.post('/api/songs', headers=headers, json={
                    'team': TEAM, 'name': role + ' MP4 등록', 'category': 'entrance',
                    'source': 'upload', 'assetId': meta['id'], 'start': 0, 'end': 0.4,
                })
                self.assertEqual(result.status, 200, await result.text())
                song = await self.store.song(TEAM, role + ' MP4 등록', 'entrance')
                self.assertEqual(song['assetId'], meta['id'])

    async def test_video_only_mp4_is_refused_and_never_becomes_an_asset(self):
        state = await self.start(self.admin, len(self.silent_mp4), '무음.mp4')
        result = await self.append(self.admin, state['id'], 0, self.silent_mp4)
        self.assertEqual(result.status, 200, await result.text())
        failed = await self.finish(self.admin, state['id'])
        self.assertEqual(failed['status'], 'error', failed)
        self.assertTrue(failed.get('error'))
        self.assertFalse(failed.get('result'))
        self.assertEqual(list(self.assets.root.glob('*.json')), [])
        self.assertEqual(list(self.assets.root.glob('*.mp4')), [])

    async def test_start_request_id_retries_resume_the_same_owned_upload_and_reject_changed_descriptors(self):
        descriptor = {'filename': 'retry.mp4', 'size': len(self.audio_mp4),
                      'requestId': '0123456789abcdef0123456789abcdef'}
        first = await self.client.post('/api/uploads', headers=self.admin, json=descriptor)
        self.assertEqual(first.status, 201, await first.text())
        state = await first.json()
        result = await self.append(self.admin, state['id'], 0, self.audio_mp4)
        self.assertEqual(result.status, 200, await result.text())
        # Simulate retrying a create whose response was lost. The byte offset
        # and identity survive; another session/reservation must not appear.
        retry = await self.client.post('/api/uploads', headers=self.admin, json=descriptor)
        self.assertEqual(retry.status, 201, await retry.text())
        retry_state = await retry.json()
        self.assertEqual(retry_state['id'], state['id'])
        self.assertEqual(retry_state['offset'], len(self.audio_mp4))
        self.assertEqual(len(list((self.settings.data_dir / 'upload-sessions').glob('*.json'))), 1)
        for changed in ({**descriptor, 'filename': 'other.mp4'},
                        {**descriptor, 'size': len(self.audio_mp4) + 1}):
            result = await self.client.post('/api/uploads', headers=self.admin, json=changed)
            await self.assert_error(result, 400)
        complete = await self.finish(self.admin, state['id'])
        self.assertEqual(complete['status'], 'complete', complete)
        again = await self.client.post('/api/uploads', headers=self.admin, json=descriptor)
        self.assertEqual(again.status, 201, await again.text())
        self.assertEqual((await again.json())['result']['id'], complete['result']['id'])
        # The caller-controlled operation key must not expose another account's
        # upload even when the same descriptor and requestId are reused.
        registrar = await self.account('registrar')
        other = await self.client.post('/api/uploads', headers=registrar, json=descriptor)
        self.assertEqual(other.status, 201, await other.text())
        other_state = await other.json()
        self.assertNotEqual(other_state['id'], state['id'])
        self.assertEqual(other_state['offset'], 0)

    async def test_fixed_chunks_retry_identically_reject_conflicts_and_finish_once(self):
        data = self.multichunk_mp4
        state = await self.start(self.admin, len(data))
        first = await self.append(self.admin, state['id'], 0, data[:CHUNK_SIZE])
        self.assertEqual(first.status, 200, await first.text())
        self.assertEqual((await self.status(self.admin, state['id']))['offset'], CHUNK_SIZE)
        duplicate = await self.append(self.admin, state['id'], 0, data[:CHUNK_SIZE])
        self.assertEqual(duplicate.status, 200, await duplicate.text())
        self.assertEqual((await duplicate.json())['offset'], CHUNK_SIZE)
        conflict = await self.append(self.admin, state['id'], 0, b'!' + data[1:CHUNK_SIZE])
        self.assertIn(conflict.status, (400, 409), await conflict.text())
        self.assertEqual((await self.status(self.admin, state['id']))['offset'], CHUNK_SIZE)
        final = await self.append(self.admin, state['id'], CHUNK_SIZE, data[CHUNK_SIZE:])
        self.assertEqual(final.status, 200, await final.text())
        complete = await self.finish(self.admin, state['id'])
        self.assertEqual(complete['status'], 'complete', complete)
        again = await self.finish(self.admin, state['id'])
        self.assertEqual(again['result']['id'], complete['result']['id'])
        self.assertEqual(len(list(self.assets.root.glob('*.json'))), 1)
        self.assertEqual(self.assets.get(complete['result']['id'])['path'].read_bytes(), data)

    async def test_wrong_chunk_sizes_and_content_types_leave_the_committed_offset_unchanged(self):
        state = await self.start(self.admin, len(self.multichunk_mp4))
        for chunk in (b'short', bytes(CHUNK_SIZE + 1)):
            result = await self.append(self.admin, state['id'], 0, chunk)
            self.assertIn(result.status, (400, 413), await result.text())
            self.assertEqual((await self.status(self.admin, state['id']))['offset'], 0)
        result = await self.client.put('/api/uploads/' + state['id'] + '?offset=0',
                                       headers=self.admin, json={'not': 'binary'})
        await self.assert_error(result, 400)
        result = await self.client.put('/api/uploads/' + state['id'] + '?offset=-1',
                                       headers={**self.admin, 'Content-Type': 'application/octet-stream'}, data=b'x')
        await self.assert_error(result, 400)
        self.assertEqual((await self.status(self.admin, state['id']))['offset'], 0)
        # A valid next chunk still works after each rejected request.
        result = await self.append(self.admin, state['id'], 0, self.multichunk_mp4[:CHUNK_SIZE])
        self.assertEqual(result.status, 200, await result.text())

    async def test_incomplete_session_cannot_finalize_or_register_an_asset(self):
        state = await self.start(self.admin, len(self.audio_mp4))
        result = await self.client.post('/api/uploads/' + state['id'] + '/complete', headers=self.admin, json={})
        await self.assert_error(result, 400)
        self.assertEqual((await self.status(self.admin, state['id']))['offset'], 0)
        self.assertEqual(list(self.assets.root.glob('*.json')), [])

    async def test_declared_ten_gib_boundary_and_bad_sizes_are_rejected_before_bytes(self):
        # start() only reserves logical capacity; it does not allocate 10 GiB.
        # Use a high reported free count so this boundary test is independent
        # of the test machine's disk. Real disk-pressure tests live separately.
        usage = shutil.disk_usage(self.settings.data_dir)
        generous = type(usage)(100 * MAX_FILE_SIZE, 0, 100 * MAX_FILE_SIZE)
        with patch('shutil.disk_usage', return_value=generous):
            exact = await self.start(self.admin, MAX_FILE_SIZE)
            result = await self.client.delete('/api/uploads/' + exact['id'], headers=self.admin)
            self.assertEqual(result.status, 200, await result.text())
            for size in (MAX_FILE_SIZE + 1, 0, -1, True, '100', 1.25):
                with self.subTest(size=size):
                    result = await self.client.post('/api/uploads', headers=self.admin,
                                                    json={'filename': 'limit.mp4', 'size': size})
                    await self.assert_error(result, 400)
        self.assertEqual(list(self.assets.root.glob('*.mp4')), [])

    async def test_json_is_still_bounded_with_and_without_content_length(self):
        payload = json.dumps({'filename': 'x' * 70000, 'size': 1}).encode()
        result = await self.client.post('/api/uploads', headers={**self.admin, 'Content-Type': 'application/json'}, data=payload)
        await self.assert_error(result, 400)
        self.assertIn('너무 큽니다', (await result.json())['error'])

        async def streamed():
            for offset in range(0, len(payload), 8192):
                yield payload[offset:offset + 8192]
        result = await self.client.post('/api/uploads', headers={**self.admin, 'Content-Type': 'application/json'}, data=streamed())
        await self.assert_error(result, 400)
        self.assertIn('너무 큽니다', (await result.json())['error'])
        self.assertLess(self.panel.app._client_max_size, 100 * 1024 * 1024)

    async def test_player_and_user_cannot_use_any_upload_session_route(self):
        state = await self.start(self.admin, len(self.audio_mp4))
        upload_path = '/api/uploads/' + state['id']
        for role in ('player', 'user'):
            headers = await self.account(role)
            for method, path, body in [
                ('POST', '/api/uploads', {'filename': 'not-allowed.mp4', 'size': 1}),
                ('GET', upload_path, None), ('PUT', upload_path + '?offset=0', None),
                ('POST', upload_path + '/complete', {}), ('DELETE', upload_path, None),
            ]:
                with self.subTest(role=role, method=method, path=path):
                    result = await self.client.request(method, path, headers=headers, json=body)
                    await self.assert_error(result, 403)
        self.assertEqual((await self.status(self.admin, state['id']))['offset'], 0)

    async def test_upload_owner_is_enforced_for_other_registrar_and_admin(self):
        owner = await self.account('registrar')
        other = await self.account('registrar', '2')
        state = await self.start(owner, len(self.audio_mp4))
        upload_path = '/api/uploads/' + state['id']
        for headers in (other, self.admin):
            for method, path, data in [
                ('GET', upload_path, None), ('PUT', upload_path + '?offset=0', self.audio_mp4),
                ('POST', upload_path + '/complete', b'{}'), ('DELETE', upload_path, None),
            ]:
                with self.subTest(owner=headers['Authorization'] == self.admin['Authorization'], method=method):
                    content_type = 'application/octet-stream' if method == 'PUT' else 'application/json'
                    result = await self.client.request(method, path, headers={**headers, 'Content-Type': content_type}, data=data)
                    await self.assert_error(result, 403)
        self.assertEqual((await self.status(owner, state['id']))['offset'], 0)

    async def test_mutations_require_csrf_and_expired_sessions_cannot_resume(self):
        owner = await self.account('registrar')
        state = await self.start(owner, len(self.audio_mp4))
        upload_path = '/api/uploads/' + state['id']
        for method, path, data in [
            ('POST', '/api/uploads', b'{"filename":"x.mp4","size":1}'),
            ('PUT', upload_path + '?offset=0', self.audio_mp4),
            ('POST', upload_path + '/complete', b'{}'), ('DELETE', upload_path, None),
        ]:
            for csrf in (None, 'invalid-csrf'):
                headers = {'Authorization': owner['Authorization'],
                           'Content-Type': 'application/octet-stream' if method == 'PUT' else 'application/json'}
                if csrf is not None:
                    headers['X-CSRF-Token'] = csrf
                result = await self.client.request(method, path, headers=headers, data=data)
                await self.assert_error(result, 403)
        result = await self.client.get(upload_path)
        await self.assert_error(result, 401)
        self.assertEqual((await self.status(owner, state['id']))['offset'], 0)
        token = owner['Authorization'].split(' ', 1)[1]
        self.panel.sessions[token]['expires'] = time.monotonic() - 1
        result = await self.client.get(upload_path, headers=owner)
        await self.assert_error(result, 401)

    async def test_cancellation_removes_incomplete_bytes_and_prevents_resume(self):
        state = await self.start(self.admin, len(self.multichunk_mp4))
        result = await self.append(self.admin, state['id'], 0, self.multichunk_mp4[:CHUNK_SIZE])
        self.assertEqual(result.status, 200, await result.text())
        result = await self.client.delete('/api/uploads/' + state['id'], headers=self.admin)
        self.assertEqual(result.status, 200, await result.text())
        cancelled = await self.status(self.admin, state['id'])
        self.assertEqual(cancelled['status'], 'error')
        self.assertIn('취소', cancelled['error'])
        result = await self.append(self.admin, state['id'], CHUNK_SIZE, self.multichunk_mp4[CHUNK_SIZE:])
        self.assertEqual(result.status, 200, await result.text())
        self.assertEqual((await result.json())['status'], 'error')
        self.assertEqual(list(self.assets.root.glob('*.json')), [])
        self.assertEqual(list(self.settings.data_dir.rglob('*.part')), [])

    async def test_legacy_small_multipart_upload_still_accepts_mp4(self):
        form = aiohttp.FormData()
        form.add_field('file', self.audio_mp4, filename='legacy.mp4', content_type='video/mp4')
        result = await self.client.post('/api/upload', headers=self.admin, data=form)
        self.assertEqual(result.status, 200, await result.text())
        meta = await result.json()
        self.assertEqual(self.assets.get(meta['id'])['path'].read_bytes(), self.audio_mp4)

    async def test_all_read_roles_can_obtain_asset_scoped_native_head_and_range(self):
        complete = await self.upload(self.admin, self.multichunk_mp4)
        asset = complete['result']
        for role in ('admin', 'registrar', 'player', 'user'):
            headers = self.admin if role == 'admin' else await self.account(role)
            with self.subTest(role=role):
                grant = await self.ticket(headers, asset['id'], download=True)
                self.assertEqual(grant['bytes'], len(self.multichunk_mp4))
                # Native browsers can HEAD/range-fetch without an Authorization
                # header; the short-lived grant is the only supplied credential.
                result = await self.client.head(grant['url'])
                self.assertEqual(result.status, 200, await result.text())
                self.assertEqual(int(result.headers['Content-Length']), len(self.multichunk_mp4))
                self.assertTrue(result.headers['Content-Disposition'].startswith('attachment;'))
                self.assertEqual(await result.read(), b'')
                # FileResponse streams; a whole-file Path.read_bytes() fallback
                # would make this bounded range request fail immediately.
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('Do not load the full media file')):
                    result = await self.client.get(grant['url'], headers={'Range': 'bytes=113-523'})
                    self.assertEqual(result.status, 206, await result.text() if result.status != 206 else '')
                    self.assertEqual(result.headers['Content-Range'], f'bytes 113-523/{len(self.multichunk_mp4)}')
                    self.assertEqual(int(result.headers['Content-Length']), 411)
                    self.assertEqual(await result.read(), self.multichunk_mp4[113:524])
                self.assertEqual(result.headers.get('Cache-Control'), 'no-store')

    async def test_forged_cross_asset_wrong_mode_and_unrelated_route_tickets_are_denied(self):
        first = (await self.upload(self.admin))['result']
        second = (await self.upload(self.admin, filename='second.mp4'))['result']
        grant = await self.ticket(self.admin, first['id'], download=False)
        parts = urlsplit(grant['url'])
        token = parse_qs(parts.query)['ticket'][0]
        cases = [
            urlunsplit(parts._replace(query=urlencode({'ticket': token + 'forged'}))),
            grant['url'].replace('/' + first['id'] + '?', '/' + second['id'] + '?'),
            grant['url'] + '&download=1',
            '/api/state?ticket=' + token,
            '/api/uploads?ticket=' + token,
        ]
        for url in cases:
            with self.subTest(url=url.split('?')[0]):
                result = await self.client.get(url)
                await self.assert_error(result, 401)
        result = await self.client.get(grant['url'])
        self.assertEqual(result.status, 200, await result.text() if result.status != 200 else '')
        self.assertEqual(result.headers['Content-Disposition'], 'inline')
        self.assertEqual(await result.read(), self.audio_mp4)
        result = await self.client.post('/api/media/' + first['id'] + '/ticket?ticket=' + token, json={})
        await self.assert_error(result, 401)

    async def test_ticket_issuance_needs_login_csrf_and_valid_boolean_mode(self):
        asset = (await self.upload(self.admin))['result']
        url = '/api/media/' + asset['id'] + '/ticket'
        for headers, expected in [({}, 401), ({'Authorization': self.admin['Authorization']}, 403),
                                  ({**self.admin, 'X-CSRF-Token': 'invalid'}, 403)]:
            result = await self.client.post(url, headers=headers, json={'download': True})
            await self.assert_error(result, expected)
        for value in ('true', 1, None):
            result = await self.client.post(url, headers=self.admin, json={'download': value})
            await self.assert_error(result, 400)
        self.assertEqual(self.panel.media_tickets, {})

    async def test_expired_ticket_and_expired_session_cannot_access_media(self):
        asset = (await self.upload(self.admin))['result']
        grant = await self.ticket(self.admin, asset['id'])
        token = parse_qs(urlsplit(grant['url']).query)['ticket'][0]
        self.panel.media_tickets[token]['expires'] = time.monotonic() - 1
        result = await self.client.get(grant['url'])
        await self.assert_error(result, 401)
        owner = await self.account('user')
        grant = await self.ticket(owner, asset['id'])
        session = owner['Authorization'].split(' ', 1)[1]
        self.panel.sessions[session]['expires'] = time.monotonic() - 1
        result = await self.client.head(grant['url'])
        self.assertEqual(result.status, 401)

    async def test_logout_and_role_revocation_invalidate_existing_media_links(self):
        asset = (await self.upload(self.admin))['result']
        owner = await self.account('user')
        grant = await self.ticket(owner, asset['id'])
        result = await self.client.post('/api/logout', headers=owner, json={})
        self.assertEqual(result.status, 200, await result.text())
        result = await self.client.get(grant['url'])
        await self.assert_error(result, 401)
        owner = await self.account('registrar')
        grant = await self.ticket(owner, asset['id'])
        result = await self.client.put('/api/users/upload_registrar', headers=self.admin, json={'role': 'pending'})
        self.assertEqual(result.status, 200, await result.text())
        result = await self.client.get(grant['url'])
        await self.assert_error(result, 401)

    async def test_ticket_lifetime_never_exceeds_the_current_session(self):
        asset = (await self.upload(self.admin))['result']
        session = self.admin['Authorization'].split(' ', 1)[1]
        self.panel.sessions[session]['expires'] = time.monotonic() + 12
        grant = await self.ticket(self.admin, asset['id'])
        token = parse_qs(urlsplit(grant['url']).query)['ticket'][0]
        self.assertLessEqual(grant['expiresIn'], 12)
        self.assertLessEqual(self.panel.media_tickets[token]['expires'], self.panel.sessions[session]['expires'])

    async def test_backup_ticket_remains_admin_only_scoped_and_preserves_original_file_bytes(self):
        asset = (await self.upload(self.admin))['result']
        for role in ('registrar', 'player', 'user'):
            headers = await self.account(role)
            result = await self.client.post('/api/backup/ticket', headers=headers, json={})
            await self.assert_error(result, 403)
        result = await self.client.post('/api/backup/ticket', json={})
        await self.assert_error(result, 401)
        result = await self.client.post('/api/backup/ticket', headers={'Authorization': self.admin['Authorization']}, json={})
        await self.assert_error(result, 403)
        result = await self.client.post('/api/backup/ticket', headers=self.admin, json={})
        self.assertEqual(result.status, 200, await result.text())
        grant = await result.json()
        self.assertEqual(urlsplit(grant['url']).path, '/api/backup')
        self.assertGreater(grant['expiresIn'], 0)
        token = parse_qs(urlsplit(grant['url']).query)['ticket'][0]
        result = await self.client.get(grant['url'])
        self.assertEqual(result.status, 200, await result.text() if result.status != 200 else '')
        self.assertIn('attachment', result.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(await result.read())) as archive:
            info = json.loads(archive.read('backup-info.json'))
            self.assertIn(asset['id'], info['includedAssetIds'])
            self.assertEqual(archive.read(info['assets'][asset['id']]['archivePath']), self.audio_mp4)
        result = await self.client.get('/api/media/' + asset['id'] + '?ticket=' + token + '&download=1')
        await self.assert_error(result, 401)
        media = await self.ticket(self.admin, asset['id'], download=True)
        media_token = parse_qs(urlsplit(media['url']).query)['ticket'][0]
        result = await self.client.get('/api/backup?ticket=' + media_token + '&download=1')
        await self.assert_error(result, 401)
        result = await self.client.get('/api/backup?ticket=' + token)
        await self.assert_error(result, 401)

    async def test_backup_space_is_checked_before_ticket_and_again_before_zip_creation(self):
        with patch('webapp.shutil.disk_usage', return_value=SimpleNamespace(free=0)), \
                patch('webapp.tempfile.TemporaryFile') as temporary, \
                patch('webapp.write_music_backup') as writer:
            result = await self.client.post('/api/backup/ticket', headers=self.admin, json={})
            await self.assert_error(result, 400)
            self.assertIn('임시 공간이 부족', (await result.json())['error'])
            self.assertEqual(self.panel.media_tickets, {})
            temporary.assert_not_called()
            writer.assert_not_called()
        result = await self.client.post('/api/backup/ticket', headers=self.admin, json={})
        self.assertEqual(result.status, 200, await result.text())
        grant = await result.json()
        # Capacity can disappear after issuance; both native-ticket and bearer
        # downloads must refuse before opening a temporary archive.
        with patch('webapp.shutil.disk_usage', return_value=SimpleNamespace(free=0)), \
                patch('webapp.tempfile.TemporaryFile') as temporary, \
                patch('webapp.write_music_backup') as writer:
            for url, headers in ((grant['url'], {}), ('/api/backup', self.admin)):
                result = await self.client.get(url, headers=headers)
                await self.assert_error(result, 400)
                self.assertIn('임시 공간이 부족', (await result.json())['error'])
            temporary.assert_not_called()
            writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
