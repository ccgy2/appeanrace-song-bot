import io
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from assets import Assets
from config import Settings
from autoplay import select_auto_song
from storage import Store
from webapp import WebPanel


def wav_bytes():
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b'\x00\x00' * 8000)
    return b.getvalue()


class PlayerAccountPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Settings(data_dir=Path(self.tmp.name), web_password='Admin-password-123456', web_only=True)
        self.store = Store(self.s)
        self.assets = Assets(self.s)
        self.bot = SimpleNamespace(settings=self.s, store=self.store, assets=self.assets, guilds=[], is_ready=lambda: False, get_guild=lambda _: None, player=SimpleNamespace(now={}))
        self.panel = WebPanel(self.bot)
        self.client = TestClient(TestServer(self.panel.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.close()
        self.tmp.cleanup()

    async def _login(self, username, password):
        r = await self.client.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(r.status, 200, await r.text())
        return (await r.json())['csrf']

    async def test_player_can_upload_and_register_song_but_not_admin_settings(self):
        password = 'Player-password-123'
        r = await self.client.post('/api/signup', json={'username': 'player1', 'displayName': '재생자1', 'password': password})
        self.assertEqual(r.status, 201, await r.text())

        csrf = await self._login('admin', self.s.web_password)
        r = await self.client.put('/api/users/player1', json={'role': 'player', 'enabled': True}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.post('/api/logout', json={}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(r.status, 200)

        csrf = await self._login('player1', password)
        headers = {'X-CSRF-Token': csrf}
        form = aiohttp.FormData()
        form.add_field('file', wav_bytes(), filename='entry.wav', content_type='audio/wav')
        r = await self.client.post('/api/upload', data=form, headers=headers)
        self.assertEqual(r.status, 200, await r.text())
        asset = await r.json()

        song = {
            'team': 'A팀', 'name': 'ID선수', 'source': 'upload', 'assetId': asset['id'],
            'start': 0, 'end': 0.8, 'memberId': '123456789012345678'
        }
        r = await self.client.post('/api/songs', json=song, headers=headers)
        self.assertEqual(r.status, 200, await r.text())
        saved = await self.store.song('A팀', 'ID선수')
        self.assertEqual(saved['memberId'], '123456789012345678')

        r = await self.client.delete('/api/songs?team=A팀&name=ID선수', headers=headers)
        self.assertEqual(r.status, 403)
        r = await self.client.post('/api/team', json={'team': '새팀'}, headers=headers)
        self.assertEqual(r.status, 403)


class AutoEntranceIdTests(unittest.TestCase):
    def test_member_id_selects_song_without_role_or_lineup(self):
        member_id = '123456789012345678'
        song = {'name': 'ID선수', 'memberId': member_id}
        picked, order = select_auto_song([song], {str(i): '' for i in range(1, 10)}, member_id, '닉네임달라도됨', False)
        self.assertIs(picked, song)
        self.assertIsNone(order)

    def test_member_id_gets_lineup_order_when_present(self):
        member_id = '223456789012345678'
        song = {'name': '선수2', 'memberId': member_id}
        picked, order = select_auto_song([song], {'3': '선수2'}, member_id, '다른이름', False)
        self.assertIs(picked, song)
        self.assertEqual(order, 3)

    def test_legacy_name_still_requires_role_and_lineup(self):
        song = {'name': '기존선수', 'memberId': ''}
        self.assertEqual(select_auto_song([song], {'1': '기존선수'}, '999', '기존선수', False), (None, None))
        self.assertEqual(select_auto_song([song], {'1': '기존선수'}, '999', '기존선수', True), (song, 1))


if __name__ == '__main__':
    unittest.main()
