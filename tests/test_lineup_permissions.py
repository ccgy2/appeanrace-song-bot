"""Lineup permission regression tests using local HTTP and temporary SQLite only.

Discord, Railway, Firebase, production accounts and uploaded music are untouched.
"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from assets import Assets
from config import Settings
from permissions import allowed, required_capability
from storage import Store
from webapp import WebPanel


PASSWORD = 'lineup-test-password-12345'
TEAM = '타순 테스트팀'
OTHER_TEAM = '다른 테스트팀'


class LineupCapabilityTests(unittest.TestCase):
    def test_lineup_is_separate_from_team_and_music_management(self):
        self.assertEqual(required_capability('/api/lineup', 'PUT'), 'lineup')
        self.assertEqual(required_capability('/api/team', 'POST'), 'teams')
        for role in ('admin', 'registrar', 'player'):
            self.assertTrue(allowed(role, 'lineup'), role)
        for role in ('user', 'pending', 'unknown', None):
            self.assertFalse(allowed(role, 'lineup'), role)
        for role in ('registrar', 'player'):
            for capability in ('teams', 'users', 'backup'):
                self.assertFalse(allowed(role, capability), (role, capability))
        self.assertTrue(allowed('registrar', 'music'))
        self.assertFalse(allowed('player', 'music'))


class LineupPermissionAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='appearance-lineup-test-')
        self.settings = Settings(data_dir=Path(self.tmp.name), web_password=PASSWORD, web_only=True)
        self.store = Store(self.settings)
        bot = SimpleNamespace(
            settings=self.settings, store=self.store, assets=Assets(self.settings),
            guilds=[], is_ready=lambda: False, get_guild=lambda _: None,
            player=SimpleNamespace(now={}),
        )
        self.panel = WebPanel(bot)
        self.client = TestClient(TestServer(self.panel.app), cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()
        self.admin = await self.login('admin')
        await self.store.create_team(TEAM)
        await self.store.save_lineup(OTHER_TEAM, {'1': '다른 팀 선수'})

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

    async def account(self, role):
        username = 'lineup_' + role
        result = await self.client.post('/api/signup', json={
            'username': username, 'displayName': role, 'password': PASSWORD,
        })
        self.assertEqual(result.status, 201, await result.text())
        result = await self.client.put('/api/users/' + username, headers=self.admin,
                                       json={'role': role, 'enabled': True})
        self.assertEqual(result.status, 200, await result.text())
        return await self.login(username)

    async def state(self, headers, team=TEAM):
        result = await self.client.get('/api/state', headers=headers, params={'team': team})
        self.assertEqual(result.status, 200, await result.text())
        return await result.json()

    async def test_registrar_and_player_save_read_and_persist_all_nine_positions(self):
        for role in ('registrar', 'player'):
            with self.subTest(role=role):
                headers = await self.account(role)
                # Fill the state cache before saving: the next read must show the new data.
                await self.state(headers)
                lineup = {str(i): f'{role} 선수 {i}' for i in range(1, 10)}
                result = await self.client.put('/api/lineup', headers=headers,
                                               json={'team': TEAM, 'lineup': lineup})
                self.assertEqual(result.status, 200, await result.text())
                self.assertEqual((await self.state(headers))['lineup'], lineup)
                self.assertEqual((await self.state(self.admin))['lineup'], lineup)
                self.assertEqual((await self.store.lineup(OTHER_TEAM))['1'], '다른 팀 선수')
                reopened = Store(self.settings)
                try:
                    self.assertEqual(await reopened.lineup(TEAM), lineup)
                finally:
                    await reopened.close()
                result = await self.client.get('/api/session', headers=headers)
                self.assertIn('lineup', (await result.json())['capabilities'])

    async def test_lineup_can_be_reordered_and_positions_cleared(self):
        headers = await self.account('player')
        await self.store.save_lineup(TEAM, {'1': '김선수', '2': '이선수', '9': '박선수'})
        lineup = {str(i): '' for i in range(1, 10)}
        lineup.update({'1': '박선수', '9': '김선수'})
        result = await self.client.put('/api/lineup', headers=headers,
                                       json={'team': TEAM, 'lineup': lineup})
        self.assertEqual(result.status, 200, await result.text())
        self.assertEqual((await self.state(headers))['lineup'], lineup)

    async def test_admin_retains_team_creation_and_default_teams_remain_editable(self):
        result = await self.client.put('/api/lineup', headers=self.admin,
                                       json={'team': '관리자가 만든 팀', 'lineup': {'1': '관리자 선수'}})
        self.assertEqual(result.status, 200, await result.text())
        self.assertIn('관리자가 만든 팀', await self.store.teams())
        self.assertEqual((await self.store.lineup('관리자가 만든 팀'))['1'], '관리자 선수')
        headers = await self.account('player')
        for team in ('A팀', '청팀', '백팀'):
            result = await self.client.put('/api/lineup', headers=headers,
                                           json={'team': team, 'lineup': {'1': '기본 팀 선수'}})
            self.assertEqual(result.status, 200, await result.text())
            self.assertEqual((await self.store.lineup(team))['1'], '기본 팀 선수')

    async def test_user_cannot_write_but_can_read_the_existing_lineup(self):
        headers = await self.account('user')
        before = await self.store.lineup(TEAM)
        result = await self.client.put('/api/lineup', headers=headers,
                                       json={'team': TEAM, 'lineup': {'1': '금지된 변경'}})
        self.assertEqual(result.status, 403, await result.text())
        self.assertEqual((await self.state(headers))['lineup'], before)

    async def test_pending_account_login_and_stale_session_are_rejected(self):
        headers = await self.account('player')
        token = headers['Authorization'].split(' ', 1)[1]
        self.panel.sessions[token]['role'] = 'pending'
        result = await self.client.put('/api/lineup', headers=headers,
                                       json={'team': TEAM, 'lineup': {'1': '금지된 변경'}})
        self.assertEqual(result.status, 403, await result.text())
        result = await self.client.put('/api/users/lineup_player', headers=self.admin,
                                       json={'role': 'pending'})
        self.assertEqual(result.status, 200, await result.text())
        result = await self.client.put('/api/lineup', headers=headers,
                                       json={'team': TEAM, 'lineup': {'1': '금지된 변경'}})
        self.assertEqual(result.status, 401, await result.text())
        result = await self.client.post('/api/login', json={
            'username': 'lineup_player', 'password': PASSWORD, 'authMode': 'bearer',
        })
        self.assertEqual(result.status, 403, await result.text())
        self.assertTrue(all(name == '' for name in (await self.store.lineup(TEAM)).values()))

    async def test_missing_auth_invalid_csrf_and_expired_session_cannot_save(self):
        headers = await self.account('registrar')
        body = {'team': TEAM, 'lineup': {'1': '금지된 변경'}}
        cases = [({}, 401), ({'Authorization': headers['Authorization']}, 403),
                 ({**headers, 'X-CSRF-Token': 'wrong-token'}, 403),
                 ({**headers, 'Authorization': 'Bearer unknown-token'}, 401)]
        for request_headers, expected in cases:
            result = await self.client.put('/api/lineup', headers=request_headers, json=body)
            self.assertEqual(result.status, expected, await result.text())
        token = headers['Authorization'].split(' ', 1)[1]
        self.panel.sessions[token]['expires'] = 0
        result = await self.client.put('/api/lineup', headers=headers, json=body)
        self.assertEqual(result.status, 401, await result.text())
        self.assertTrue(all(name == '' for name in (await self.store.lineup(TEAM)).values()))

    async def test_lineup_permission_does_not_allow_team_creation_users_or_backups(self):
        for role in ('registrar', 'player'):
            headers = await self.account(role)
            for method, path, body in [
                ('POST', '/api/team', {'team': '관리자 전용 팀'}),
                ('POST', '/api/team', {'team': TEAM, 'activate': True, 'guildId': '123'}),
                ('GET', '/api/users', None),
                ('PUT', '/api/users/lineup_' + role, {'role': 'admin'}),
                ('GET', '/api/export', None),
                ('GET', '/api/backup', None),
                ('PUT', '/api/lineup', {'team': '타순으로 만든 새 팀', 'lineup': {'1': '선수'}}),
            ]:
                with self.subTest(role=role, method=method, path=path):
                    result = await self.client.request(method, path, headers=headers, json=body)
                    self.assertEqual(result.status, 403, await result.text())
            self.assertNotIn('관리자 전용 팀', await self.store.teams())
            self.assertNotIn('타순으로 만든 새 팀', await self.store.teams())

    async def test_player_still_cannot_edit_music_or_shared_event_definitions(self):
        headers = await self.account('player')
        for method, path, body in [
            ('POST', '/api/songs', {'team': TEAM, 'name': '변경 금지'}),
            ('DELETE', '/api/songs?team=타순%20테스트팀&name=곡', None),
            ('POST', '/api/upload', {}),
            ('POST', '/api/events', {'team': TEAM, 'label': '변경 금지'}),
            ('PUT', '/api/events', {'team': TEAM, 'key': 'homerun', 'tracks': []}),
            ('DELETE', '/api/events?team=타순%20테스트팀&key=homerun', None),
        ]:
            result = await self.client.request(method, path, headers=headers, json=body)
            self.assertEqual(result.status, 403, (method, path, await result.text()))

    async def test_invalid_positions_and_payloads_do_not_overwrite_saved_lineup(self):
        headers = await self.account('registrar')
        await self.store.save_lineup(TEAM, {'1': '기존 선수'})
        before = await self.store.lineup(TEAM)
        for lineup in ({'10': '잘못된 타순'}, ['선수'], None):
            result = await self.client.put('/api/lineup', headers=headers,
                                           json={'team': TEAM, 'lineup': lineup})
            self.assertEqual(result.status, 400, await result.text())
            self.assertEqual(await self.store.lineup(TEAM), before)


if __name__ == '__main__':
    unittest.main()
