"""Studio update: real local API/SQLite/file tests; Discord and YouTube explicitly mocked.
No cloud credentials, production database or real Discord connection is used.
"""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from assets import Assets
from backups import write_music_backup, original_filename
from config import Settings, ROOT
from event_tracks import event_tracks, MAX_EVENT_TRACKS
from permissions import allowed, discord_role
from storage import Store, PLAYBACK_CACHE
from webapp import WebPanel
from test_core import wav_bytes
from test_voice import voice, discord, setup_pair, client

# Load actual Player implementation, substituting only external Discord/YT packages.
ytdlp_stub=ModuleType('yt_dlp');ytdlp_stub.utils=SimpleNamespace(DownloadError=RuntimeError)
with patch.dict(sys.modules,{'discord':discord,'yt_dlp':ytdlp_stub,'voice':voice}):
    spec=importlib.util.spec_from_file_location('studio_test_player',ROOT/'player.py')
    pm=importlib.util.module_from_spec(spec);spec.loader.exec_module(pm)
rspec=importlib.util.spec_from_file_location('restore_test',ROOT/'scripts/restore_audio_backup.py')
restore_module=importlib.util.module_from_spec(rspec);rspec.loader.exec_module(restore_module)

PASSWORD='test-only-studio-123456'
def song(category='entrance',name='같은 이름'):
    return {'name':name,'category':category,'source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20,'memberId':''}

class StudioAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Settings(data_dir=Path(self.tmp.name),web_password=PASSWORD,web_only=True)
        self.store=Store(self.s);self.assets=Assets(self.s)
        self.bot=SimpleNamespace(settings=self.s,store=self.store,assets=self.assets,guilds=[],is_ready=lambda:False,get_guild=lambda _:None,player=SimpleNamespace(now={}))
        self.panel=WebPanel(self.bot);self.client=TestClient(TestServer(self.panel.app),cookie_jar=aiohttp.DummyCookieJar());await self.client.start_server()
        self.admin=await self.login('admin')
    async def asyncTearDown(self):
        await self.client.close();await self.store.close();self.tmp.cleanup()
    async def login(self,name):
        r=await self.client.post('/api/login',json={'username':name,'password':PASSWORD,'authMode':'bearer'})
        self.assertEqual(r.status,200,await r.text());d=await r.json()
        return {'Authorization':'Bearer '+d['accessToken'],'X-CSRF-Token':d['csrf']}
    async def account(self,role):
        name='account_'+role
        r=await self.client.post('/api/signup',json={'username':name,'displayName':role,'password':PASSWORD});self.assertEqual(r.status,201,await r.text())
        r=await self.client.put('/api/users/'+name,headers=self.admin,json={'role':role,'enabled':True});self.assertEqual(r.status,200,await r.text())
        return await self.login(name)
    async def upload(self,name='우리 팀 응원가.wav',headers=None):
        data=aiohttp.FormData(quote_fields=False);data.add_field('file',wav_bytes(),filename=name,content_type='audio/wav')
        r=await self.client.post('/api/upload',data=data,headers=headers or self.admin);self.assertEqual(r.status,200,await r.text());return await r.json()
    async def save_three(self):
        for c in ['entrance','cheer','situation']:await self.store.save_song('팀',song(c))
    async def state(self):
        r=await self.client.get('/api/state?team=팀',headers=self.admin);self.assertEqual(r.status,200,await r.text());return await r.json()
    async def test_role_matrix_denies_direct_http_mutations(self):
        for role in ['player','user']:
            h=await self.account(role)
            for method,path,body in [('POST','/api/songs',{'team':'팀',**song()}),('DELETE','/api/songs?team=팀&name=곡',None),('PUT','/api/events',{'team':'팀','key':'homerun','tracks':[]}),('POST','/api/events',{'team':'팀','label':'새 상황'}),('DELETE','/api/events?team=팀&key=homerun',None),('POST','/api/team',{'team':'팀'}),('PUT','/api/lineup',{'team':'팀','lineup':{}}),('GET','/api/backup',None),('GET','/api/users',None)]:
                r=await self.client.request(method,path,json=body,headers=h);self.assertEqual(r.status,403,(role,method,path,await r.text()))
            data=aiohttp.FormData();data.add_field('file',wav_bytes(),filename='no.wav')
            r=await self.client.post('/api/upload',data=data,headers=h);self.assertEqual(r.status,403)
            r=await self.client.get('/api/state',headers=h);self.assertEqual(r.status,200)
            r=await self.client.put('/api/playback-settings',json={'enabled':False},headers=h);self.assertEqual(r.status,200 if role=='player' else 403)
            if role=='user':
                r=await self.client.post('/api/control',json={'action':'stop'},headers=h);self.assertEqual(r.status,403)
    async def test_registrar_can_manage_all_event_sources(self):
        h=await self.account('registrar');await self.save_three();asset=await self.upload(headers=h)
        tracks=[{'type':'song','category':c,'songName':'같은 이름'} for c in ['entrance','cheer','situation']]+[{'type':'asset','assetId':asset['id']}]
        r=await self.client.put('/api/events',json={'team':'팀','key':'homerun','tracks':tracks+tracks,'playMode':'sequence'},headers=h)
        self.assertEqual(r.status,200,await r.text());d=await self.store.event('팀','homerun');self.assertEqual(len(d['tracks']),4)
        self.assertEqual([x.get('category') for x in d['tracks'][:3]],['entrance','cheer','situation']);self.assertEqual(d['playMode'],'sequence')
        self.assertEqual(d['tracks'][3]['filename'],'우리 팀 응원가.wav')
        r=await self.client.delete('/api/events?team=팀&key=homerun',headers=h);self.assertEqual(r.status,200)
        r=await self.client.put('/api/events',json={'team':'팀','key':'homerun','restore':True},headers=h);self.assertEqual(r.status,200)
        self.assertEqual((await self.store.event('팀','homerun'))['tracks'],d['tracks'])
        r=await self.client.get('/api/backup',headers=h);self.assertEqual(r.status,403)
    async def test_role_downgrade_revokes_existing_session(self):
        h=await self.account('registrar')
        r=await self.client.put('/api/users/account_registrar',json={'role':'user'},headers=self.admin);self.assertEqual(r.status,200)
        r=await self.client.get('/api/state',headers=h);self.assertEqual(r.status,401)
        h=await self.login('account_registrar');r=await self.client.get('/api/session',headers=h);d=await r.json();self.assertEqual(d['capabilities'],['read'])
        r=await self.client.put('/api/users/account_registrar',json={'role':'superuser'},headers=self.admin);self.assertEqual(r.status,400)
    async def test_toggle_boolean_csrf_durable_and_shared_bots(self):
        r=await self.client.get('/api/playback-settings',headers=self.admin);self.assertTrue((await r.json())['autoEntrance']['enabled'])
        for value in ['false',0,None,[],{}]:
            r=await self.client.put('/api/playback-settings',headers=self.admin,json={'enabled':value});self.assertEqual(r.status,400)
        r=await self.client.put('/api/playback-settings',headers={'Authorization':self.admin['Authorization']},json={'enabled':False});self.assertEqual(r.status,403)
        second=Store(self.s)
        try:
            off=await self.store.set_auto_entrance(False);self.assertFalse(second.playback_snapshot()['enabled'])
            old=off['revision'];on=await second.set_auto_entrance(True);self.assertGreater(on['revision'],old);self.assertEqual(on,self.store.playback_snapshot())
            await self.store.set_auto_entrance(False);PLAYBACK_CACHE.pop(self.store.playback_cache_key)
            self.assertFalse((await second.playback_settings())['enabled'])
            with patch.object(self.store,'_read',side_effect=AssertionError('live must not read DB')):
                self.assertFalse(self.store.playback_snapshot()['enabled'])
            self.assertFalse((await self.state())['autoEntrance']['enabled'])
        finally:await second.close()
    async def test_builtin_delete_survives_reload_and_other_team_unaffected(self):
        r=await self.client.delete('/api/events?team=팀&key=homerun',headers=self.admin);self.assertEqual(r.status,200)
        d=await self.state();self.assertNotIn('homerun',[x['key'] for x in d['events']]);self.assertIn('homerun',[x['key'] for x in d['deletedEvents']])
        r=await self.client.get('/api/state?team=다른팀',headers=self.admin);self.assertIn('homerun',[x['key'] for x in (await r.json())['events']])
        r=await self.client.put('/api/events',json={'team':'팀','key':'homerun','tracks':[]},headers=self.admin);self.assertEqual(r.status,400)
        r=await self.client.put('/api/events',json={'team':'팀','key':'homerun','restore':True},headers=self.admin);self.assertEqual(r.status,200)
        self.assertIn('homerun',[x['key'] for x in (await self.state())['events']])
    async def test_category_rename_delete_references_and_legacy(self):
        await self.save_three()
        tracks=[{'type':'song','category':c,'songName':'같은 이름'} for c in ['entrance','cheer','situation']]
        await self.store.set_event('팀','homerun',{'tracks':tracks,'deleted':True,'playMode':'sequence'})
        await self.store.save_song('팀',song('cheer','바뀐 응원가'),old_name='같은 이름')
        d=await self.store.event('팀','homerun');self.assertEqual([t['songName'] for t in d['tracks']],['같은 이름','바뀐 응원가','같은 이름'])
        await self.store.delete_song('팀','같은 이름','entrance');d=await self.store.event('팀','homerun');self.assertTrue(d['deleted']);self.assertEqual(len(d['tracks']),2)
        await self.store.delete_song('팀','바뀐 응원가','cheer');await self.store.delete_song('팀','같은 이름','situation')
        d=await self.store.event('팀','homerun');self.assertTrue(d['deleted']);self.assertEqual(d.get('tracks',[]),[])
        self.assertEqual(event_tracks({'tracks':[{'type':'song','songName':'옛날 곡'}]})[0]['category'],'situation')
    async def test_event_validation_atomic_limit_and_invalid_category(self):
        await self.save_three();before={'tracks':[{'type':'song','category':'cheer','songName':'같은 이름'}],'playMode':'random'};await self.store.set_event('팀','homerun',before)
        for tracks in [[{'type':'song','category':'invalid','songName':'같은 이름'}],[{'type':'song','category':'entrance','songName':'없음'}],[{'type':'asset','assetId':'../x'}],[None],before['tracks']*(MAX_EVENT_TRACKS+1)]:
            r=await self.client.put('/api/events',json={'team':'팀','key':'homerun','tracks':tracks},headers=self.admin);self.assertEqual(r.status,400,await r.text());self.assertEqual(await self.store.event('팀','homerun'),before)
    async def test_backup_original_unicode_names_duplicates_and_restore(self):
        a=await self.upload();b=await self.upload();c=await self.upload('literal%20name.wav')
        await self.store.set_event('팀','homerun',{'tracks':[{'type':'asset','assetId':'f'*32}],'deleted':True})
        r=await self.client.get('/api/backup',headers=self.admin);self.assertEqual(r.status,200);data=await r.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn('uploads/우리 팀 응원가.wav',z.namelist());self.assertIn('uploads/우리 팀 응원가 (2).wav',z.namelist());self.assertIn('uploads/literal%20name.wav',z.namelist())
            info=json.loads(z.read('backup-info.json'));self.assertIn('f'*32,info['missingAssetIds'])
            for aid in [a['id'],b['id'],c['id']]:self.assertEqual(z.read(info['assets'][aid]['archivePath']),wav_bytes())
            self.assertNotIn('passwordHash',z.read('metadata.json').decode())
        backup=Path(self.tmp.name)/'backup.zip';backup.write_bytes(data);dest=Path(self.tmp.name)/'restored'
        with patch('builtins.print'):
            self.assertEqual(restore_module.restore(backup,dest),3);self.assertFalse(dest.exists())
            self.assertEqual(restore_module.restore(backup,dest,True),3)
            self.assertEqual(restore_module.restore(backup,dest,True),3)
        recovered=Assets(Settings(data_dir=dest))
        for aid in [a['id'],b['id'],c['id']]:self.assertEqual(recovered.get(aid)['path'].read_bytes(),wav_bytes())
        self.assertFalse((dest/'songs.sqlite3').exists())
    async def test_public_pwa_assets_and_no_private_caching(self):
        for path in ['/download.html','/offline.html','/pwa.js','/sw.js','/manifest.webmanifest','/icons/icon-192.png','/icons/icon-512.png','/icons/apple-touch-icon.png']:
            r=await self.client.get(path);self.assertEqual(r.status,200,(path,await r.text() if 'image/' not in r.content_type else 'image'));self.assertIn('no-store',r.headers['Cache-Control'])
        r=await self.client.get('/manifest.webmanifest');self.assertEqual(r.content_type,'application/manifest+json');self.assertEqual((await r.json(content_type=None))['start_url'],'/')
        r=await self.client.get('/sw.js');self.assertEqual(r.headers['Service-Worker-Allowed'],'/')
        r=await self.client.get('/icons/secret.txt');self.assertEqual(r.status,404)
        sw=(ROOT/'static/sw.js').read_text();self.assertIn("url.pathname.startsWith('/api/')",sw);self.assertNotIn("cache.put",sw)
    async def test_stage_channels_exposed_alongside_voice(self):
        guild=SimpleNamespace(id=123,name='Mock guild',me=SimpleNamespace(voice=None),voice_client=None,voice_channels=[],stage_channels=[])
        perms=SimpleNamespace(view_channel=True,connect=True,speak=False,mute_members=True)
        channel=SimpleNamespace(id=789,name='스테이지',members=[],permissions_for=lambda _:perms);guild.stage_channels=[channel]
        self.bot.guilds=[guild];self.bot.is_ready=lambda:True;self.bot.get_guild=lambda _:guild;self.bot.player.voice=SimpleNamespace(status=lambda _:{'connected':False})
        r=await self.client.get('/api/state?team=팀&guildId=123',headers=self.admin);self.assertEqual(r.status,200,await r.text());d=await r.json()
        self.assertEqual(d['channels'][0]['type'],'stage');self.assertTrue(d['channels'][0]['available']);self.assertTrue(d['channels'][0]['stageAutoSpeaker'])

class StageTests(unittest.IsolatedAsyncioTestCase):
    def stage(self,mute=False,suppressed=True,request=True):
        g,ch,perms=setup_pair();ch.type=discord.ChannelType.stage_voice;perms.speak=False;perms.mute_members=mute;perms.request_to_speak=request
        g.me=SimpleNamespace(voice=SimpleNamespace(channel=ch,suppress=suppressed,mute=False),edit=AsyncMock(),request_to_speak=AsyncMock())
        vc=client(g,ch);g.voice_client=vc
        async def edit(**kwargs):g.me.voice.suppress=kwargs['suppress']
        g.me.edit.side_effect=edit
        return g,ch,vc
    async def test_already_speaker_reuses_connection(self):
        g,ch,vc=self.stage(suppressed=False);m=voice.VoiceManager();self.assertIs(await m.connect(g,ch),vc);g.me.edit.assert_not_called()
    async def test_auto_speaker_with_mute_members_permission(self):
        g,ch,vc=self.stage(mute=True);m=voice.VoiceManager();self.assertIs(await m.connect(g,ch),vc);g.me.edit.assert_awaited_once_with(suppress=False);self.assertFalse(g.me.voice.suppress)
    async def test_without_permission_requests_once_stays_connected(self):
        g,ch,vc=self.stage();m=voice.VoiceManager()
        for _ in range(2):
            with self.assertRaisesRegex(voice.VoiceError,'청중'):await m.connect(g,ch)
        g.me.request_to_speak.assert_awaited_once();vc.disconnect.assert_not_called();g.me.edit.assert_not_called()
        g.me.voice.suppress=False;self.assertIs(await m.connect(g,ch),vc)
    async def test_promotion_gateway_timeout_fails_before_audio(self):
        g,ch,vc=self.stage(mute=True);g.me.edit.side_effect=None;m=voice.VoiceManager()
        with patch.object(voice.asyncio,'sleep',new=AsyncMock()):
            with self.assertRaisesRegex(voice.VoiceError,'확인되지'):await m.connect(g,ch)
        vc.disconnect.assert_not_called()
    async def test_stage_no_connect_permission_is_blocked(self):
        g,ch,_=self.stage();ch.permissions_for(g.me).connect=False
        with self.assertRaisesRegex(voice.VoiceError,'연결'):await voice.VoiceManager().connect(g,ch)

class PlayerBehavior(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Settings(data_dir=Path(self.tmp.name));self.store=Store(self.s)
        self.player=pm.Player(self.s,self.store,Assets(self.s));self.guild=SimpleNamespace(id=42,me=SimpleNamespace(voice=None))
        self.player.voice.connect=AsyncMock(return_value=object());self.player._start=Mock()
        self.player._extract_limited=AsyncMock(return_value={'url':'mock-audio','duration':60})
        self.patches=[patch.object(discord,'PCMVolumeTransformer',Mock(),create=True),patch.object(discord,'FFmpegPCMAudio',Mock(),create=True)]
        for p in self.patches:p.start()
    async def asyncTearDown(self):
        for p in self.patches:p.stop()
        await self.store.close();self.tmp.cleanup()
    async def test_manual_play_works_while_global_auto_off(self):
        await self.store.set_auto_entrance(False);self.assertTrue(await self.player.play(self.guild,'팀',song()));self.player._start.assert_called_once()
    async def test_auto_off_never_prepares_connects_or_starts(self):
        setting=await self.store.set_auto_entrance(False);self.assertFalse(await self.player.play(self.guild,'팀',song(),auto_revision=setting['revision']))
        self.player._extract_limited.assert_not_called();self.player.voice.connect.assert_not_called();self.player._start.assert_not_called()
    async def test_off_then_on_cancels_old_preparation_revision(self):
        setting=await self.store.playback_settings();begun=asyncio.Event();release=asyncio.Event()
        async def prepare(url):begun.set();await release.wait();return {'url':'mock','duration':60}
        self.player._extract_limited.side_effect=prepare
        task=asyncio.create_task(self.player.play(self.guild,'팀',song(),auto_revision=setting['revision']));await begun.wait()
        await self.store.set_auto_entrance(False);await self.store.set_auto_entrance(True);release.set();self.assertFalse(await task)
        self.player.voice.connect.assert_not_called();self.player._start.assert_not_called()
    async def test_off_during_connect_does_not_start_ffmpeg(self):
        setting=await self.store.playback_settings()
        async def connect(*args):await self.store.set_auto_entrance(False);return object()
        self.player.voice.connect.side_effect=connect
        self.assertFalse(await self.player.play(self.guild,'팀',song(),auto_revision=setting['revision']));self.player._start.assert_not_called()
    async def test_event_sequence_all_categories_and_per_guild(self):
        tracks=[]
        for c in ['entrance','cheer','situation']:
            await self.store.save_song('팀',song(c));tracks.append({'type':'song','category':c,'songName':'같은 이름'})
        await self.store.set_event('팀','homerun',{'tracks':tracks,'playMode':'sequence'})
        self.player.play=AsyncMock(return_value=True)
        for c in ['entrance','cheer','situation','entrance']:
            await self.player.event(self.guild,'팀','homerun');self.assertEqual(self.player.play.call_args.args[2]['category'],c)
        await self.player.event(SimpleNamespace(id=43),'팀','homerun');self.assertEqual(self.player.play.call_args.args[2]['category'],'entrance')
    async def test_deleted_event_not_playable(self):
        await self.store.set_event('팀','homerun',{'deleted':True})
        with self.assertRaisesRegex(ValueError,'삭제'):await self.player.event(self.guild,'팀','homerun')
        self.player.voice.connect.assert_not_called()

class PureChecks(unittest.TestCase):
    def test_discord_roles_hierarchy_and_legacy(self):
        def member(*names):return SimpleNamespace(id=7,guild_permissions=SimpleNamespace(administrator=False),roles=[SimpleNamespace(name=n) for n in names])
        for names,role in [((),'user'),(('등장곡 재생인',),'player'),(('등장곡 재생자',),'player'),(('등장곡 등록/삭제자','등장곡 재생자'),'registrar')]:self.assertEqual(discord_role(member(*names)),role)
        self.assertEqual(discord_role(member(),7),'admin');self.assertFalse(allowed('unknown','read'));self.assertFalse(allowed('user','play'));self.assertTrue(allowed('registrar','events'));self.assertFalse(allowed('registrar','users'))
    def test_safe_backup_filename(self):
        self.assertEqual(original_filename('../원본 곡.wav','.wav'),'원본 곡.wav');self.assertEqual(original_filename('CON.wav','.wav'),'_CON.wav');self.assertEqual(original_filename('50%20곡.wav','.wav'),'50%20곡.wav')

if __name__=='__main__':unittest.main()
