"""Cost-profile and diagnostics integration without production credentials."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from assets import Assets
from config import Settings
from player import Player
from release import RELEASE
from storage import Store
from webapp import WebPanel


class EconomyIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name), web_only=True,
                                 web_password='test-economy-password', audio_profile='economy')
        self.store = Store(self.settings)
        self.assets = Assets(self.settings)
        self.bot = SimpleNamespace(settings=self.settings, store=self.store, assets=self.assets,
                                   guilds=[], is_ready=lambda:False, player=SimpleNamespace(now={}))
        self.panel = WebPanel(self.bot)
        self.panel.resource_meter.snapshot = Mock(return_value={
            'available':True, 'process':{'rssBytes':40 * 1024 * 1024},
            'container':{'available':False, 'isRailwayBilling':False},
            'children':{'count':0, 'byType':[]}, 'minimumRefreshSeconds':5})
        self.client = TestClient(TestServer(self.panel.app), cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()
        self.admin = await self.login('admin')

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.close()
        self.temp.cleanup()

    async def login(self, name):
        response = await self.client.post('/api/login', json={
            'username':name, 'password':self.settings.web_password, 'authMode':'bearer'})
        self.assertEqual(response.status, 200, await response.text())
        value = await response.json()
        return {'Authorization':'Bearer ' + value['accessToken'], 'X-CSRF-Token':value['csrf']}

    async def test_admin_sees_release_profile_and_on_demand_resources(self):
        response = await self.client.get('/api/diagnostics', headers=self.admin)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data['release'], RELEASE)
        self.assertEqual(data['audioProfile'], 'economy')
        self.assertEqual(data['audioProfileInfo']['bitrateKbps'], 96)
        self.assertFalse(data['audioProfileInfo']['forwardErrorCorrection'])
        self.assertEqual(data['resources']['process']['rssBytes'], 40 * 1024 * 1024)
        self.panel.resource_meter.snapshot.assert_called_once()
        self.assertIn('no-store', response.headers['Cache-Control'])

    async def test_ordinary_read_user_does_not_trigger_or_receive_meter(self):
        admin = await self.store.web_user('admin')
        await self.store.save_web_user('viewer', {
            'passwordHash':admin['passwordHash'], 'role':'user', 'enabled':True, 'displayName':'viewer'})
        headers = await self.login('viewer')
        response = await self.client.get('/api/diagnostics', headers=headers)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertNotIn('resources', data)
        self.assertEqual(data['audioProfile'], 'economy')
        self.panel.resource_meter.snapshot.assert_not_called()

    async def test_public_connection_identifies_release_without_resources(self):
        response = await self.client.get('/api/connection')
        data = await response.json()
        self.assertEqual(data['release'], RELEASE)
        self.assertNotIn('resources', data)
        self.assertNotIn('passwordHash', str(data))
        self.panel.resource_meter.snapshot.assert_not_called()

    async def test_player_passes_public_economy_options_and_keeps_volume_metadata(self):
        player = Player(self.settings, self.store, self.assets)
        source = SimpleNamespace(appearance_song_percent=150, appearance_master_volume=.6, cleanup=Mock())
        guild = SimpleNamespace(id=5)
        vc = SimpleNamespace(is_playing=lambda:False, is_paused=lambda:False, play=Mock())
        try:
            player._start(guild, vc, source, 0, '곡', '팀')
            options = vc.play.call_args.kwargs
            self.assertEqual(options['bitrate'], 96)
            self.assertFalse(options['fec'])
            self.assertEqual(options['signal_type'], 'music')
            self.assertEqual(player.now[5]['songVolumePercent'], 150)
            self.assertEqual(player.now[5]['masterVolumePercent'], 60)
        finally:
            await player.close()

    async def test_environment_profile_validation(self):
        env = {'WEB_ONLY':'true', 'WEB_ADMIN_PASSWORD':self.settings.web_password,
               'DATA_DIR':self.temp.name, 'AUDIO_PROFILE':'economy'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Settings.from_env().audio_profile, 'economy')
        env['AUDIO_PROFILE'] = 'invalid'
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(ValueError, 'AUDIO_PROFILE'):
            Settings.from_env()


if __name__ == '__main__':
    unittest.main()
