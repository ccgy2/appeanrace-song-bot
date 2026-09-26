"""Per-song gain patch: local SQLite/HTTP tests and mocked Discord playback.
No Discord token, production database, Railway deploy or external YouTube is used.
"""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from assets import Assets
from autoplay import select_auto_song
from backups import write_music_backup
from config import Settings
from event_tracks import event_tracks
from storage import Store
from validation import validate_song, volume_percent, saved_volume_percent
from webapp import WebPanel
import test_studio_update as studio
from test_core import wav_bytes

CATS = ('entrance', 'cheer', 'situation')
def song(category='entrance', **extra):
    return {**studio.song(category, '테스트 곡'), **extra}

class VolumeValidation(unittest.TestCase):
    def test_valid_integer_bounds_and_numeric_input(self):
        for v in [0, 1, 50, 100, 175, 200, '0', '120', 150.0]:
            with self.subTest(v=v):
                self.assertEqual(volume_percent(v), int(v))
                self.assertEqual(validate_song(song(volumePercent=v))['volumePercent'], int(v))
    def test_invalid_values_never_saved(self):
        for v in [-1, 201, 1.5, 'NaN', float('nan'), float('inf'), None, True, False, '', ' ', [], {}, '1;rm', 10**400]:
            with self.subTest(v=str(v)[:25]):
                with self.assertRaises(ValueError): validate_song(song(volumePercent=v))
    def test_omission_is_distinct_from_explicit_zero(self):
        self.assertNotIn('volumePercent', validate_song(song()))
        self.assertEqual(validate_song(song(volumePercent=0))['volumePercent'], 0)
    def test_read_only_default_handles_old_or_bad_imports(self):
        for record in [None, {}, {'volumePercent': None}, {'volumePercent': True}, {'volumePercent': -1}, {'volumePercent': 'bad'}]:
            self.assertEqual(saved_volume_percent(record), 100)
        self.assertEqual(saved_volume_percent({'volumePercent': 0}), 0)
    def test_event_normalization_retains_zero_and_legacy_default(self):
        old = {'assetId': 'a'*32, 'filename': '원본.wav', 'volumePercent': 0}
        self.assertEqual(event_tracks(old)[0]['volumePercent'], 0)
        self.assertEqual(event_tracks({'assetId': 'b'*32})[0]['volumePercent'], 100)
        self.assertNotIn('volumePercent', event_tracks({'songName': '곡', 'volumePercent': 200})[0])

class VolumeStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.store = Store(self.settings)
    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()
    async def test_old_record_read_defaults_without_writing_or_migrating(self):
        path='teams/팀/entranceSongs/테스트 곡'
        await self.store._run(self.store._write, path, {'source':'youtube','url':song()['url'],'start':0,'end':20})
        self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],100)
        self.assertEqual((await self.store.songs('팀'))[0]['volumePercent'],100)
        self.assertNotIn('volumePercent', await self.store._run(self.store._read,path))
    async def test_new_record_defaults_to_100_and_master_is_independent(self):
        await self.store.set_volume('팀', .37)
        await self.store.save_song('팀', song())
        self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],100)
        await self.store.save_song('팀', song(volumePercent=180))
        self.assertEqual((await self.store.state('팀'))['volume'], .37)
    async def test_old_client_update_preserves_gain_and_zero(self):
        for value in [0,175]:
            await self.store.save_song('팀',song(volumePercent=value))
            await self.store.save_song('팀',validate_song(song(end=15)))
            self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],value)
    async def test_categories_and_teams_with_same_name_are_independent(self):
        for c,v in zip(CATS,[0,80,170]): await self.store.save_song('팀',song(c,volumePercent=v))
        await self.store.save_song('다른팀',song(volumePercent=125))
        self.assertEqual([(await self.store.song('팀','테스트 곡',c))['volumePercent'] for c in CATS],[0,80,170])
        self.assertEqual((await self.store.song('다른팀','테스트 곡'))['volumePercent'],125)
    async def test_rename_keeps_gain_id_lineup_and_event_reference(self):
        await self.store.save_song('팀',song(volumePercent=165,memberId='123456789012345678'))
        await self.store.save_lineup('팀',{'1':'테스트 곡'})
        await self.store.set_event('팀','homerun',{'tracks':[{'type':'song','category':'entrance','songName':'테스트 곡'}]})
        await self.store.save_song('팀',validate_song(song(name='새 이름',memberId='123456789012345678')),old_name='테스트 곡')
        saved=await self.store.song('팀','새 이름')
        self.assertEqual(saved['volumePercent'],165);self.assertEqual(saved['memberId'],'123456789012345678')
        self.assertEqual((await self.store.lineup('팀'))['1'],'새 이름')
        self.assertEqual((await self.store.event('팀','homerun'))['tracks'][0]['songName'],'새 이름')
    async def test_reload_and_export_preserve_settings(self):
        await self.store.save_song('팀',song(volumePercent=0))
        await self.store.set_event('팀','homerun',{'volumePercent':160})
        other=Store(self.settings)
        try:
            self.assertEqual((await other.song('팀','테스트 곡'))['volumePercent'],0)
            exported=await other.export()
            self.assertEqual(exported['teams']['팀']['songs'][0]['volumePercent'],0)
            self.assertEqual(exported['teams']['팀']['events'][0]['volumePercent'],160)
        finally: await other.close()
    async def test_internal_writer_rejects_invalid_gain_before_mutation(self):
        await self.store.save_song('팀',song(volumePercent=80))
        with self.assertRaises(ValueError): await self.store.save_song('팀',song(volumePercent=999))
        self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],80)

class VolumeAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=Settings(data_dir=Path(self.tmp.name),web_password=studio.PASSWORD,web_only=True)
        self.store=Store(self.settings);self.assets=Assets(self.settings)
        self.bot=SimpleNamespace(settings=self.settings,store=self.store,assets=self.assets,guilds=[],is_ready=lambda:False,get_guild=lambda _:None,player=SimpleNamespace(now={}))
        self.panel=WebPanel(self.bot);self.client=TestClient(TestServer(self.panel.app),cookie_jar=aiohttp.DummyCookieJar());await self.client.start_server()
        self.admin=await self.login('admin')
    async def asyncTearDown(self):
        await self.client.close();await self.store.close();self.tmp.cleanup()
    async def login(self,name):
        r=await self.client.post('/api/login',json={'username':name,'password':studio.PASSWORD,'authMode':'bearer'})
        self.assertEqual(r.status,200,await r.text());d=await r.json()
        return {'Authorization':'Bearer '+d['accessToken'],'X-CSRF-Token':d['csrf']}
    async def account(self,role):
        base=await self.store.web_user('admin')
        await self.store.save_web_user('test_'+role,{'passwordHash':base['passwordHash'],'role':role,'enabled':True,'displayName':role})
        return await self.login('test_'+role)
    async def save(self,headers=None,**extra):
        return await self.client.post('/api/songs',headers=headers or self.admin,json={'team':'팀',**song(**extra)})
    async def upload(self,name='원래 등록한 이름.wav'):
        data=aiohttp.FormData(quote_fields=False);data.add_field('file',wav_bytes(),filename=name,content_type='audio/wav')
        r=await self.client.post('/api/upload',headers=self.admin,data=data);self.assertEqual(r.status,200,await r.text());return await r.json()
    async def event(self,headers=None,**data):
        return await self.client.put('/api/events',headers=headers or self.admin,json={'team':'팀','key':'homerun',**data})
    async def test_new_capability_and_state_flag(self):
        r=await self.client.get('/api/connection');d=await r.json();self.assertIn('song-volume',d['capabilities'])
        r=await self.client.get('/api/state?team=팀',headers=self.admin);self.assertTrue((await r.json())['songVolumeEnabled'])
    async def test_all_library_types_save_gain_through_http(self):
        for c,v in zip(CATS,[0,80,170]):
            r=await self.save(category=c,volumePercent=v);self.assertEqual(r.status,200,await r.text())
        r=await self.client.get('/api/state?team=팀',headers=self.admin);d=await r.json()
        self.assertEqual([d['library'][c][0]['volumePercent'] for c in CATS],[0,80,170])
        self.assertEqual(d['state']['volume'],.5)
    async def test_api_denies_bad_values_without_overwriting_old_record(self):
        await self.save(volumePercent=75)
        for v in [None,False,-10,201,12.5,'NaN','inf',{},[]]:
            r=await self.save(volumePercent=v);self.assertEqual(r.status,400,await r.text())
            self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],75)
    async def test_registrar_allowed_player_and_user_rejected(self):
        for role in ['registrar','player','user']:
            h=await self.account(role);r=await self.save(headers=h,volumePercent=120)
            self.assertEqual(r.status,200 if role=='registrar' else 403)
            r=await self.event(headers=h,volumePercent=75)
            self.assertEqual(r.status,200 if role=='registrar' else 403)
    async def test_csrf_and_auth_required(self):
        r=await self.save(headers={'Authorization':self.admin['Authorization']},volumePercent=125);self.assertEqual(r.status,403)
        r=await self.client.post('/api/songs',json={'team':'팀',**song(volumePercent=125)});self.assertEqual(r.status,401)
    async def test_uploaded_and_missing_original_records_can_keep_gain(self):
        asset=await self.upload();payload={'source':'upload','assetId':asset['id'],'start':0,'end':1}
        r=await self.save(**payload,volumePercent=150);self.assertEqual(r.status,200)
        path=self.assets.get(asset['id'])['path'];before=hashlib.sha256(path.read_bytes()).hexdigest()
        r=await self.save(**payload,volumePercent=0,oldName='테스트 곡');self.assertEqual(r.status,200)
        self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())
        path.unlink()
        r=await self.save(**payload,volumePercent=80,oldName='테스트 곡');self.assertEqual(r.status,200,await r.text())
        self.assertIn('원본 오디오',(await r.json())['warning'])
        self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],80)
    async def test_direct_event_files_keep_independent_gains_when_old_client_reorders(self):
        a,b=await self.upload('첫째.wav'),await self.upload('둘째.wav')
        r=await self.event(tracks=[{'type':'asset','assetId':a['id'],'volumePercent':0},{'type':'asset','assetId':b['id'],'volumePercent':175}],playMode='sequence')
        self.assertEqual(r.status,200,await r.text())
        r=await self.event(tracks=[{'type':'asset','assetId':b['id']},{'type':'asset','assetId':a['id']}],playMode='random')
        self.assertEqual(r.status,200)
        self.assertEqual([t['volumePercent'] for t in (await self.store.event('팀','homerun'))['tracks']],[175,0])
        r=await self.event(assetId=a['id']);self.assertEqual(r.status,200)
        self.assertEqual((await self.store.event('팀','homerun'))['tracks'][0]['volumePercent'],0)
    async def test_invalid_event_gain_does_not_erase_event(self):
        a=await self.upload();track={'type':'asset','assetId':a['id'],'volumePercent':80}
        await self.event(tracks=[track]);previous=await self.store.event('팀','homerun')
        for bad in [False,201,-5,1.5,'nan']:
            r=await self.event(tracks=[{**track,'volumePercent':bad}]);self.assertEqual(r.status,400)
            self.assertEqual(await self.store.event('팀','homerun'),previous)
    async def test_library_track_never_overrides_or_doubles_stored_song_gain(self):
        await self.save(volumePercent=150)
        track={'type':'song','category':'entrance','songName':'테스트 곡'}
        r=await self.event(tracks=[track],volumePercent=200);self.assertEqual(r.status,200)
        self.assertNotIn('volumePercent',(await self.store.event('팀','homerun'))['tracks'][0])
        r=await self.event(tracks=[{**track,'volumePercent':50}]);self.assertEqual(r.status,400)
        self.assertEqual((await self.store.song('팀','테스트 곡'))['volumePercent'],150)
    async def test_default_sound_zero_reset_and_delete_restore(self):
        r=await self.event(tracks=[],volumePercent=0);self.assertEqual(r.status,200)
        self.assertEqual((await self.store.event('팀','homerun'))['volumePercent'],0)
        r=await self.client.delete('/api/events?team=팀&key=homerun',headers=self.admin);self.assertEqual(r.status,200)
        r=await self.event(restore=True);self.assertEqual(r.status,200)
        self.assertEqual((await self.store.event('팀','homerun'))['volumePercent'],0)
        r=await self.event(tracks=[],volumePercent=100);self.assertEqual(r.status,200)
        self.assertIsNone(await self.store.event('팀','homerun'))
    async def test_legacy_named_file_gain_only_retains_path(self):
        await self.store.set_event('팀','homerun',{'file':'homerun1.mp3'})
        r=await self.event(volumePercent=130);self.assertEqual(r.status,200)
        self.assertEqual(await self.store.event('팀','homerun'),{'file':'homerun1.mp3','volumePercent':130})
    async def test_backup_and_media_remain_original_with_separate_gain_metadata(self):
        a=await self.upload()
        await self.save(source='upload',assetId=a['id'],start=0,end=1,volumePercent=170)
        await self.event(tracks=[{'type':'asset','assetId':a['id'],'volumePercent':60}])
        r=await self.client.get('/api/media/'+a['id']+'?download=1',headers=self.admin)
        self.assertEqual(await r.read(),wav_bytes())
        r=await self.client.get('/api/backup',headers=self.admin);self.assertEqual(r.status,200,await r.text() if r.status!=200 else '')
        with zipfile.ZipFile(io.BytesIO(await r.read())) as z:
            self.assertEqual(z.read('uploads/원래 등록한 이름.wav'),wav_bytes())
            data=json.loads(z.read('metadata.json'))['teams']['팀']
            self.assertEqual(data['library']['entrance'][0]['volumePercent'],170)
            self.assertEqual(data['events'][0]['tracks'][0]['volumePercent'],60)
    async def test_save_invalidates_cached_state(self):
        await self.save(volumePercent=80)
        await self.client.get('/api/state?team=팀',headers=self.admin)
        await self.save(volumePercent=150)
        r=await self.client.get('/api/state?team=팀',headers=self.admin)
        self.assertEqual((await r.json())['songs'][0]['volumePercent'],150)

# These source objects replace Discord transport; assertions inspect exact gain and flow.
class FakePCM:
    def __init__(self,source,volume=1): self.original=source;self.volume=volume
    def cleanup(self): self.original.cleanup()
class FakeFFmpeg:
    def __init__(self,target,**kwargs): self.target=target;self.options=kwargs
    def cleanup(self): pass

class VolumePlayback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=Settings(data_dir=Path(self.tmp.name));self.store=Store(self.settings)
        self.assets=Assets(self.settings);self.player=studio.pm.Player(self.settings,self.store,self.assets)
        self.guild=SimpleNamespace(id=42,me=SimpleNamespace(voice=None),voice_client=None)
        vc=SimpleNamespace(source=None,after=None,is_playing=Mock(return_value=False),is_paused=Mock(return_value=False),stop=Mock())
        def play(source,after=None):vc.source=source;vc.after=after;vc.is_playing.return_value=True
        vc.play=Mock(side_effect=play);self.vc=vc;self.guild.voice_client=vc
        self.player.voice.connect=AsyncMock(return_value=vc)
        self.player._extract_limited=AsyncMock(return_value={'url':'https://mock.invalid/audio','duration':60})
        self.patches=[patch.object(studio.discord,'PCMVolumeTransformer',FakePCM,create=True),patch.object(studio.discord,'FFmpegPCMAudio',FakeFFmpeg,create=True)]
        for p in self.patches:p.start()
    async def asyncTearDown(self):
        await self.player.close()
        for p in reversed(self.patches):p.stop()
        await self.store.close();self.tmp.cleanup()
    async def test_all_categories_use_master_times_song_percent_once(self):
        await self.store.set_volume('팀',.7)
        for c in CATS:
            await self.player.play(self.guild,'팀',song(c,volumePercent=120))
            self.assertAlmostEqual(self.vc.source.volume,.84)
            self.assertEqual(self.player.now[42]['songVolumePercent'],120)
            self.assertEqual(self.player.now[42]['masterVolumePercent'],70)
    async def test_mute_default_and_boost_bounds(self):
        for master,pct in [(0,200),(.5,0),(.5,100),(1,200)]:
            await self.store.set_volume('팀',master)
            await self.player.play(self.guild,'팀',song(volumePercent=pct))
            self.assertAlmostEqual(self.vc.source.volume,master*pct/100)
        await self.player.play(self.guild,'팀',song())
        self.assertAlmostEqual(self.vc.source.volume,1)
    async def test_master_adjustments_retain_track_gain_without_restart(self):
        await self.player.play(self.guild,'팀',song(volumePercent=180))
        playing=self.vc.source
        for v in [0,25,50,100]:
            await self.player.volume(self.guild,'팀',v)
            self.assertIs(self.vc.source,playing);self.assertAlmostEqual(playing.volume,v/100*1.8)
        self.vc.play.assert_called_once()
    async def test_silent_track_stays_silent_after_master_change(self):
        await self.player.play(self.guild,'팀',song(volumePercent=0))
        await self.player.volume(self.guild,'팀',100)
        self.assertEqual(self.vc.source.volume,0)
    async def test_next_track_does_not_inherit_previous_track_gain(self):
        await self.player.play(self.guild,'팀',song(volumePercent=200))
        await self.player.play(self.guild,'팀',song(name='다음 곡'))
        self.assertEqual(self.vc.source.appearance_song_percent,100);self.assertEqual(self.vc.source.volume,.5)
    async def test_editing_another_teams_master_does_not_touch_current_audio(self):
        await self.player.play(self.guild,'팀',song(volumePercent=150));source=self.vc.source
        await self.player.volume(self.guild,'다른팀',10)
        self.assertEqual(source.volume,.75);self.assertEqual((await self.store.state('다른팀'))['volume'],.1)
    async def test_uploaded_preview_and_automatic_id_song_use_same_gain(self):
        member='123456789012345678';entry=song(source='upload',assetId='a'*32,volumePercent=125,memberId=member,start=0,end=10)
        with patch.object(self.assets,'get',return_value={'duration':30,'path':Path('/mock/audio.wav')}):
            selected,order=select_auto_song([entry],{},member,'이름',False)
            self.assertIs(selected,entry)
            auto=await self.store.playback_settings()
            await self.player.play(self.guild,'팀',selected,preview=True,auto_revision=auto['revision'])
            self.assertEqual(self.vc.source.volume,.625)
            self.assertIn('-t 5.000',self.vc.source.original.options['options'])
            self.player._extract_limited.assert_not_called()
    async def test_linked_event_looks_up_current_library_gain_without_double_gain(self):
        for c,v in zip(CATS,[80,125,170]):
            await self.store.save_song('팀',song(c,volumePercent=v))
            await self.store.set_event('팀','homerun',{'volumePercent':200,'tracks':[{'type':'song','category':c,'songName':'테스트 곡'}]})
            await self.player.event(self.guild,'팀','homerun');self.assertAlmostEqual(self.vc.source.volume,.5*v/100)
            await self.store.save_song('팀',song(c,volumePercent=60))
            await self.player.event(self.guild,'팀','homerun');self.assertAlmostEqual(self.vc.source.volume,.3)
    async def test_raw_event_sequence_gains_and_gain_edit_does_not_reset_order(self):
        tracks=[{'type':'asset','assetId':'a'*32,'volumePercent':80},{'type':'asset','assetId':'b'*32,'volumePercent':160}]
        with patch.object(self.assets,'get',side_effect=lambda ident:{'path':Path('/mock/'+ident+'.wav')}):
            await self.store.set_event('팀','homerun',{'tracks':tracks,'playMode':'sequence'})
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.vc.source.volume,.4)
            tracks[0]['volumePercent']=0
            await self.store.set_event('팀','homerun',{'tracks':tracks,'playMode':'sequence'})
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.vc.source.volume,.8)
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.vc.source.volume,0)
    async def test_random_raw_event_uses_selected_file_gain(self):
        tracks=[{'type':'asset','assetId':'a'*32,'volumePercent':40},{'type':'asset','assetId':'b'*32,'volumePercent':170}]
        await self.store.set_event('팀','homerun',{'tracks':tracks,'playMode':'random'})
        with patch.object(studio.pm.random,'choice',side_effect=lambda items:items[1]),patch.object(self.assets,'get',return_value={'path':Path('/mock/a.wav')}):
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.vc.source.volume,.85)
    async def test_default_and_legacy_files_use_configured_gain(self):
        with patch.object(self.assets,'bundled',return_value=[Path('/mock/default.wav')]),patch.object(self.assets,'legacy_file',return_value=Path('/mock/legacy.wav')):
            await self.store.set_event('팀','homerun',{'volumePercent':0})
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.vc.source.volume,0)
            await self.store.set_event('팀','homerun',{'file':'legacy.wav','volumePercent':160})
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.vc.source.volume,.8)
    async def test_gain_save_does_not_interrupt_audio_applies_at_next_play(self):
        await self.store.save_song('팀',song(volumePercent=80));await self.player.play(self.guild,'팀',await self.store.song('팀','테스트 곡'))
        source=self.vc.source
        await self.store.save_song('팀',song(volumePercent=160))
        self.assertEqual(source.volume,.4);self.vc.play.assert_called_once()
        await self.player.play(self.guild,'팀',await self.store.song('팀','테스트 곡'))
        self.assertEqual(self.vc.source.volume,.8)
    async def test_second_bot_gets_saved_gain_without_shared_source_state(self):
        other=studio.pm.Player(self.settings,self.store,self.assets)
        other.voice.connect=AsyncMock(return_value=SimpleNamespace(is_playing=lambda:False,is_paused=lambda:False,play=Mock()))
        other._extract_limited=AsyncMock(return_value={'url':'https://mock.invalid/audio','duration':60})
        await self.store.save_song('팀',song(volumePercent=150));entry=await self.store.song('팀','테스트 곡')
        await self.player.play(self.guild,'팀',entry);await other.play(self.guild,'팀',entry)
        self.assertEqual(self.player.now[42]['songVolumePercent'],150);self.assertEqual(other.now[42]['songVolumePercent'],150)
        await other.close()

if __name__=='__main__':unittest.main()
