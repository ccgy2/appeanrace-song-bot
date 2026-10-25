"""Bound authentication memory/CPU without weakening password hashing."""
import asyncio
from collections import deque
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from assets import Assets
from config import Settings
from storage import Store
from webapp import WebPanel, PBKDF2_ROUNDS


PASSWORD = 'auth-resource-fixture-1234'


class AuthResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name), web_password=PASSWORD, web_only=True)
        self.store = Store(self.settings)
        bot = SimpleNamespace(settings=self.settings, store=self.store, assets=Assets(self.settings),
                              guilds=[], is_ready=lambda: False, get_guild=lambda _: None,
                              player=SimpleNamespace(now={}))
        self.panel = WebPanel(bot)
        self.client = TestClient(TestServer(self.panel.app), cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.close()
        self.temp.cleanup()

    async def login(self):
        return await self.client.post('/api/login', json={'username': 'admin', 'password': PASSWORD,
                                                          'authMode': 'bearer'})

    async def test_cleanup_removes_only_expired_sessions_and_buckets(self):
        now = time.monotonic()
        self.panel.sessions = {'expired': {'expires': now - 1}, 'active': {'expires': now + 500}}
        self.panel.attempts['old'] = deque([now - 400])
        self.panel.attempts['active'] = deque([now - 1])
        self.panel.signup_attempts['empty'] = deque()
        self.panel.signup_attempts['active'] = deque([now - 2])
        self.panel._cleanup_auth(force=True)
        self.assertEqual(set(self.panel.sessions), {'active'})
        self.assertEqual(set(self.panel.attempts), {'active'})
        self.assertEqual(set(self.panel.signup_attempts), {'active'})

    async def test_cleanup_is_periodic_on_traffic_without_timer_task(self):
        self.panel._cleanup_auth(force=True)
        self.panel.sessions['just-expired'] = {'expires': time.monotonic() - 1}
        self.panel._cleanup_auth()
        self.assertIn('just-expired', self.panel.sessions)
        self.panel.next_auth_cleanup = 0
        await self.client.get('/healthz')
        self.assertNotIn('just-expired', self.panel.sessions)

    async def test_ip_bucket_capacity_rejects_new_keys_then_reclaims_old(self):
        now = time.monotonic()
        with patch('webapp.AUTH_MAX_IP_BUCKETS', 2):
            self.panel._limit_login(SimpleNamespace(remote='one'))
            self.panel._limit_login(SimpleNamespace(remote='two'))
            with self.assertRaises(web.HTTPTooManyRequests):
                self.panel._limit_login(SimpleNamespace(remote='three'))
            self.assertEqual(set(self.panel.attempts), {'one', 'two'})
            self.panel.attempts['one'] = deque([now - 400])
            self.panel._limit_login(SimpleNamespace(remote='three'))
            self.assertEqual(set(self.panel.attempts), {'two', 'three'})

    async def test_signup_bucket_is_generous_and_separate_from_login(self):
        request = SimpleNamespace(remote='shared-proxy')
        for _ in range(60):
            self.panel._limit_signup(request)
        with self.assertRaises(web.HTTPTooManyRequests):
            self.panel._limit_signup(request)
        self.panel._limit_login(request)
        self.assertEqual(len(self.panel.attempts['shared-proxy']), 1)

    async def test_rate_limit_precedes_admin_bootstrap_and_preserves_retry_hint(self):
        self.panel._ensure_admin = AsyncMock()
        now = time.monotonic()
        self.panel.attempts['127.0.0.1'] = deque([now] * 10)
        response = await self.login()
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers['Retry-After'], '300')
        self.assertIn('로그인 시도', (await response.json())['error'])
        self.panel.signup_attempts['127.0.0.1'] = deque([now] * 60)
        response = await self.client.post('/api/signup', json={})
        self.assertEqual(response.status, 429)
        self.panel._ensure_admin.assert_not_called()

    async def test_capacity_preserves_active_logins_and_reclaims_expiry(self):
        with patch('webapp.AUTH_MAX_SESSIONS', 2):
            responses = [await self.login(), await self.login()]
            self.assertTrue(all(r.status == 200 for r in responses))
            tokens = [(await r.json())['accessToken'] for r in responses]
            response = await self.login()
            self.assertEqual(response.status, 429)
            self.assertEqual(set(self.panel.sessions), set(tokens))
            self.panel.sessions[tokens[0]]['expires'] = time.monotonic() - 1
            response = await self.login()
            self.assertEqual(response.status, 200)
            self.assertIn(tokens[1], self.panel.sessions)
            self.assertNotIn(tokens[0], self.panel.sessions)
            self.assertEqual(len(self.panel.sessions), 2)

    async def test_password_strength_and_normal_signup_unchanged(self):
        response = await self.client.post('/api/signup', json={'username': 'new_player',
                                    'displayName': '테스트 선수', 'password': PASSWORD})
        self.assertEqual(response.status, 201)
        user = await self.store.web_user('new_player')
        self.assertTrue(user['passwordHash'].startswith(f'pbkdf2_sha256${PBKDF2_ROUNDS}$'))
        self.assertEqual(user['role'], 'pending')
        self.assertEqual((await self.login()).status, 200)

    async def test_hash_slots_survive_request_cancellation_until_thread_finishes(self):
        release = threading.Event()
        lock = threading.Lock()
        active = peak = started = 0
        def slow_work():
            nonlocal active, peak, started
            with lock:
                active += 1
                started += 1
                peak = max(peak, active)
            try:
                release.wait(3)
                return True
            finally:
                with lock:
                    active -= 1
        tasks = [asyncio.create_task(self.panel._password_work(slow_work)) for _ in range(3)]
        try:
            async with asyncio.timeout(2):
                while started < 2:
                    await asyncio.sleep(.005)
            tasks[0].cancel()
            await asyncio.sleep(.02)
            self.assertEqual(started, 2)
            self.assertFalse(tasks[0].done(), 'cancelled CPU work released its resource slot early')
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(peak, 2)
        self.assertEqual(self.panel.auth_hash_pending, 0)

    async def test_hash_waiting_queue_has_capacity(self):
        release = threading.Event()
        tasks = []
        with patch('webapp.AUTH_MAX_PENDING_HASHES', 2):
            try:
                tasks = [asyncio.create_task(self.panel._password_work(release.wait, 3)) for _ in range(2)]
                await asyncio.sleep(.02)
                with self.assertRaises(web.HTTPTooManyRequests):
                    await self.panel._password_work(lambda: True)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertEqual(self.panel.auth_hash_pending, 0)

    async def test_request_admission_bounds_bootstrap_lock_waiters(self):
        release = asyncio.Event()
        with patch('webapp.AUTH_MAX_REQUESTS', 1):
            first = asyncio.create_task(self.panel._auth_request(release.wait))
            await asyncio.sleep(0)
            try:
                with self.assertRaises(web.HTTPTooManyRequests):
                    await self.panel._auth_request(release.wait)
            finally:
                release.set()
                await first
        self.assertEqual(self.panel.auth_requests, 0)


if __name__ == '__main__':
    unittest.main()
