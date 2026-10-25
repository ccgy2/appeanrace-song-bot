"""Resource regressions: real SQLite/API, no cloud credentials or voice traffic."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from assets import Assets
from config import Settings
from storage import Store
from validation import SONG_CATEGORIES
from webapp import WebPanel, response


def song(name='선수', category='entrance', **extra):
    return {'name': name, 'category': category, 'source': 'youtube',
            'url': 'https://www.youtube.com/watch?v=abcdefghijk',
            'start': 0, 'end': 20, 'memberId': '', 'volumePercent': 125, **extra}


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Settings(data_dir=Path(self.tmp.name))
        self.store = Store(self.s)

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_snapshot_matches_existing_readers_and_legacy_records(self):
        for category in SONG_CATEGORIES:
            await self.store.save_song('팀_%한글', song(category=category, custom='보존'))
        await self.store.save_lineup('팀_%한글', {'1': '선수', '9': '선수'})
        await self.store.set_volume('팀_%한글', .7)
        await self.store.set_order('팀_%한글', 9)
        await self.store.set_event('팀_%한글', 'homerun', {'tracks': [], 'deleted': True})
        await self.store.set_team(123, '팀_%한글')
        # The legacy team has children but no parent document.
        await self.store._run(self.store._write, 'teams/옛팀/entranceSongs/기존선수',
                              {'url': song()['url'], 'start': 0, 'end': 20})
        snapshot = await self.store.dashboard_snapshot('', 123)
        self.assertEqual(snapshot['teams'], await self.store.teams())
        self.assertEqual(snapshot['activeTeam'], await self.store.get_team(123))
        self.assertEqual(snapshot['team'], '팀_%한글')
        self.assertEqual(snapshot['library'], await self.store.library('팀_%한글'))
        self.assertEqual(snapshot['lineup'], await self.store.lineup('팀_%한글'))
        self.assertEqual(snapshot['state'], await self.store.state('팀_%한글'))
        self.assertEqual(snapshot['events'], {x['id']: x for x in await self.store.events('팀_%한글')})
        self.assertEqual(snapshot['revision'], self.store.revision)

    async def test_sqlite_snapshot_uses_one_dispatch_two_indexed_reads(self):
        await self.store.save_song('선택팀', song())
        for i in range(20):
            await self.store.save_song(f'다른팀{i}', song())
        statements = []
        self.store.sql.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, '_run', wraps=self.store._run) as dispatch:
                data = await self.store.dashboard_snapshot('선택팀')
        finally:
            self.store.sql.set_trace_callback(None)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(len([s for s in statements if s.startswith('SELECT')]), 2)
        self.assertEqual([s['name'] for s in data['library']['entrance']], ['선수'])
        plan = self.store.sql.execute('EXPLAIN QUERY PLAN SELECT path,body FROM docs WHERE path>=? AND path<?',
                                      ('teams/선택팀/', 'teams/선택팀0')).fetchall()
        self.assertIn('USING INDEX', ' '.join(str(x) for x in plan))

    async def test_prefix_ranges_preserve_literal_wildcards_and_collection_depth(self):
        for team in ['팀_%', '팀_다른팀', '팀_%다른', '팀_😀']:
            await self.store.save_song(team, song(name=team))
        await self.store._run(self.store._write, 'teams/팀_%/entranceSongs/외부/하위', {'name': '하위'})
        data = await self.store.dashboard_snapshot('팀_%')
        self.assertEqual([s['name'] for s in data['library']['entrance']], ['팀_%'])
        self.assertEqual([s['name'] for s in await self.store.songs('팀_%')], ['팀_%'])

    async def test_secondary_team_default_and_requested_team_stay_distinct(self):
        self.assertEqual((await self.store.dashboard_snapshot('', 123, 'secondary'))['team'], '백팀')
        await self.store.set_team(123, '원정팀', 'secondary')
        data = await self.store.dashboard_snapshot('임시보기', 123, 'secondary')
        self.assertEqual((data['team'], data['activeTeam']), ('임시보기', '원정팀'))

    async def test_revision_shared_only_for_same_database_and_failed_write_not_published(self):
        sibling = Store(self.s)
        with tempfile.TemporaryDirectory() as other_dir:
            other = Store(replace(self.s, data_dir=Path(other_dir)))
            try:
                old = self.store.revision
                await sibling.set_volume('팀', .3)
                self.assertGreater(self.store.revision, old)
                self.assertEqual(self.store.revision, sibling.revision)
                self.assertEqual(other.revision, 0)
                old = self.store.revision
                sibling.settings = replace(self.s, on_railway=True)
                with self.assertRaises(ValueError):
                    await sibling.set_volume('팀', .8)
                self.assertEqual(self.store.revision, old)
                self.assertEqual((await self.store.state('팀'))['volume'], .3)
            finally:
                await other.close()
                await sibling.close()

    async def test_library_uses_one_executor_dispatch(self):
        with patch.object(self.store, '_run', wraps=self.store._run) as dispatch:
            library = await self.store.library('팀')
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(set(library), set(SONG_CATEGORIES))

    async def test_existing_team_merge_does_not_commit_again(self):
        await self.store.create_team('팀')
        revision = self.store.revision
        statements = []
        self.store.sql.set_trace_callback(statements.append)
        try:
            await self.store.create_team('팀')
        finally:
            self.store.sql.set_trace_callback(None)
        self.assertEqual(self.store.revision, revision)
        self.assertFalse(any(s.startswith(('INSERT', 'COMMIT')) for s in statements))
        self.assertIn('팀', await self.store.teams())


class ResourceAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Settings(data_dir=Path(self.tmp.name), web_password='test-only-resource-123', web_only=True)
        self.store, self.assets = Store(self.s), Assets(self.s)
        self.bot = SimpleNamespace(settings=self.s, store=self.store, assets=self.assets, guilds=[],
                                   is_ready=lambda: False, get_guild=lambda _: None, player=SimpleNamespace(now={}))
        self.panel = WebPanel(self.bot)
        self.client = TestClient(TestServer(self.panel.app), cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()
        result = await self.client.post('/api/login', json={'password': self.s.web_password, 'authMode': 'bearer'})
        self.assertEqual(result.status, 200)
        login = await result.json()
        self.headers = {'Authorization': 'Bearer ' + login['accessToken'], 'X-CSRF-Token': login['csrf']}

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.close()
        self.tmp.cleanup()

    async def state(self, team='팀'):
        result = await self.client.get('/api/state', params={'team': team}, headers=self.headers)
        self.assertEqual(result.status, 200, await result.text())
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        return await result.json()

    async def test_concurrent_tabs_fill_cache_once_and_deduplicate_asset_metadata(self):
        for category in SONG_CATEGORIES:
            await self.store.save_song('팀', song(category=category, source='upload', assetId='a' * 32))
        with patch.object(self.store, 'dashboard_snapshot', wraps=self.store.dashboard_snapshot) as snapshot, \
                patch.object(self.assets, 'get', return_value={'name': '공유파일.mp3', 'duration': 60}) as asset:
            results = await asyncio.gather(*(self.state() for _ in range(12)))
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(asset.call_count, 1)
        self.assertTrue(all(result['library']['cheer'][0]['filename'] == '공유파일.mp3' for result in results))

    async def test_discord_or_peer_store_write_invalidates_cached_dashboard_immediately(self):
        await self.store.save_song('팀', song(volumePercent=80))
        self.assertEqual((await self.state())['songs'][0]['volumePercent'], 80)
        sibling = Store(self.s)
        try:
            await sibling.save_song('팀', song(volumePercent=160))
            await sibling.set_order('팀', 4)
            current = await self.state()
            self.assertEqual(current['songs'][0]['volumePercent'], 160)
            self.assertEqual(current['state']['currentOrder'], 4)
        finally:
            await sibling.close()

    async def test_cache_hits_and_live_do_not_dispatch_database_work(self):
        await self.state()
        with patch.object(self.store, '_run', side_effect=AssertionError('unexpected database work')):
            await self.state()
            result = await self.client.get('/api/live', headers=self.headers)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.headers['Cache-Control'], 'no-store')

    async def test_cache_capacity_is_bounded_and_expiry_reloads(self):
        for i in range(12):
            await self.state(f'팀{i}')
        self.assertEqual(len(self.panel.state_cache), 8)
        self.panel.state_cache_ttl = 0
        with patch.object(self.store, 'dashboard_snapshot', wraps=self.store.dashboard_snapshot) as snapshot:
            await self.state('팀11')
        self.assertEqual(snapshot.call_count, 1)

    async def test_missing_asset_keeps_original_records(self):
        await self.store.save_song('팀', song(source='upload', assetId='b' * 32))
        current = await self.state()
        self.assertEqual(current['songs'][0]['assetId'], 'b' * 32)
        self.assertTrue(current['songs'][0]['fileMissing'])
        self.assertEqual((await self.store.song('팀', '선수'))['assetId'], 'b' * 32)


class JSONTests(unittest.TestCase):
    def test_json_is_compact_without_changing_unicode_or_values(self):
        data = {'곡 이름': '공백 포함 이름', '배열': [1, True, None, {'값': '한글'}]}
        compact = response(data).text
        self.assertEqual(json.loads(compact), data)
        self.assertLess(len(compact.encode()), len(json.dumps(data, ensure_ascii=False).encode()))


if __name__ == '__main__':
    unittest.main()
