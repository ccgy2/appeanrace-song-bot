"""Three libraries, atomic rename, persisted files and protected backup; no cloud credentials."""
import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import wave
import zipfile

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from config import Settings
from assets import Assets
from storage import Store
from persistence import storage_status, require_upload_storage
from validation import validate_song, SONG_CATEGORIES
from autoplay import select_auto_song
from webapp import WebPanel

PASSWORD = 'local-test-password-1234'
MEMBER = '123456789012345678'
URL = 'https://youtu.be/abcdefghijk'

def wav():
    b = io.BytesIO()
    with wave.open(b, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000); f.writeframes(b'\0\0' * 24000)
    return b.getvalue()


def song(name='김선수', category='entrance', **extra):
    return validate_song({'name': name, 'category': category, 'source':'youtube', 'url':URL,
                          'start':0, 'end':2, 'memberId':MEMBER, **extra})


class PersistenceConfigTests(unittest.TestCase):
    def test_railway_without_volume_cannot_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Settings(data_dir=Path(tmp),on_railway=True)
            self.assertFalse(storage_status(s)['uploadsAllowed'])
            with self.assertRaisesRegex(ValueError, 'Volume'): require_upload_storage(s)

    def test_directory_named_data_is_not_a_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Settings(data_dir=Path(tmp),volume_path=Path(tmp),on_railway=True)
            with patch('persistence.is_mount_path',return_value=False):
                self.assertFalse(storage_status(s)['persistent'])
                with self.assertRaises(ValueError):require_upload_storage(s)

    def test_real_mount_containing_data_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Settings(data_dir=Path(tmp)/'app-data',volume_path=Path(tmp),on_railway=True)
            with patch('persistence.is_mount_path',return_value=True):
                self.assertTrue(storage_status(s)['persistent']);require_upload_storage(s)

    def test_path_prefix_is_not_containment(self):
        s=Settings(data_dir=Path('/data2'),volume_path=Path('/data'),on_railway=True)
        with patch('persistence.is_mount_path',return_value=True):
            self.assertFalse(storage_status(s)['uploadsAllowed'])

    def test_volume_autoselected_without_changing_explicit_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            volume=Path(tmp)/'vol'; volume.mkdir()
            env={'WEB_ONLY':'true','WEB_ADMIN_PASSWORD':PASSWORD,'RAILWAY_VOLUME_MOUNT_PATH':str(volume),
                 'APP_DOCKER_DATA_DIR':'/data'}
            with patch.dict(os.environ,env,clear=True):
                s=Settings.from_env();self.assertEqual(s.data_dir,volume);self.assertTrue(s.on_railway)
            with patch.dict(os.environ,{**env,'DATA_DIR':str(Path(tmp)/'old')},clear=True):
                s=Settings.from_env();self.assertEqual(s.data_dir,Path(tmp)/'old')
                self.assertFalse(storage_status(s)['uploadsAllowed'])

    def test_local_directory_not_claimed_cloud_persistent(self):
        d=storage_status(Settings())
        self.assertIsNone(d['persistent']);self.assertTrue(d['uploadsAllowed'])


class LibraryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Settings(data_dir=Path(self.tmp.name));self.store=Store(self.s)
    async def asyncTearDown(self):
        await self.store.close();self.tmp.cleanup()

    async def test_same_name_in_three_categories_survives_reopen(self):
        for category in SONG_CATEGORIES:await self.store.save_song('팀',song(category=category))
        await self.store.close();self.store=Store(self.s)
        for category,rows in (await self.store.library('팀')).items():
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['category'],category)
        self.assertEqual(len(await self.store.songs('팀')),1)

    async def test_legacy_collection_and_user_account_remain(self):
        await self.store._run(self.store._write,'teams/옛팀/entranceSongs/기존닉',{'url':URL,'start':0,'end':2,'memberId':MEMBER,'custom':'keep'})
        await self.store.save_web_user('existing',{'role':'player','passwordHash':'unchanged'})
        await self.store.save_song('옛팀',song('새닉'),old_name='기존닉')
        d=await self.store.song('옛팀','새닉')
        self.assertEqual(d['custom'],'keep');self.assertEqual(d['memberId'],MEMBER)
        self.assertEqual((await self.store.web_user('existing'))['passwordHash'],'unchanged')

    async def test_rename_updates_lineup_not_other_categories(self):
        for c in SONG_CATEGORIES:await self.store.save_song('팀',song(category=c))
        await self.store.save_lineup('팀',{'1':'김선수','9':'김선수'})
        await self.store.save_song('팀',song('새닉'),old_name='김선수')
        self.assertIsNone(await self.store.song('팀','김선수'))
        self.assertEqual((await self.store.lineup('팀'))['9'],'새닉')
        self.assertIsNotNone(await self.store.song('팀','김선수','cheer'))
        self.assertEqual((await self.store.song('팀','새닉'))['memberId'],MEMBER)

    async def test_rename_collision_preserves_original_and_lineup(self):
        for name in ['김선수','겹침']:await self.store.save_song('팀',song(name))
        await self.store.save_lineup('팀',{'1':'김선수'})
        with self.assertRaises(ValueError):await self.store.save_song('팀',song('겹침'),old_name='김선수')
        self.assertEqual((await self.store.lineup('팀'))['1'],'김선수')
        self.assertEqual(len(await self.store.songs('팀')),2)

    async def test_stale_edit_does_not_create_duplicate(self):
        with self.assertRaises(ValueError):await self.store.save_song('팀',song('새닉'),old_name='사라진닉')
        self.assertEqual(await self.store.songs('팀'),[])

    async def test_renaming_situation_updates_soundboard_and_delete_restores_default(self):
        await self.store.save_song('팀',song('홈런곡','situation'))
        await self.store.set_event('팀','homerun',{'songName':'홈런곡','category':'situation'})
        await self.store.save_song('팀',song('새홈런곡','situation'),old_name='홈런곡')
        self.assertEqual((await self.store.event('팀','homerun'))['songName'],'새홈런곡')
        await self.store.delete_song('팀','새홈런곡','situation')
        self.assertIsNone(await self.store.event('팀','homerun'))

    async def test_deleting_cheer_does_not_remove_entrance_or_lineup(self):
        for c in SONG_CATEGORIES:await self.store.save_song('팀',song(category=c))
        await self.store.save_lineup('팀',{'1':'김선수'})
        await self.store.delete_song('팀','김선수','cheer')
        self.assertEqual((await self.store.lineup('팀'))['1'],'김선수')
        self.assertIsNotNone(await self.store.song('팀','김선수'))
        self.assertIsNone(await self.store.song('팀','김선수','cheer'))

    async def test_unsafe_railway_local_metadata_changes_are_not_acknowledged(self):
        self.store.settings=replace(self.s,on_railway=True)
        with self.assertRaises(ValueError):await self.store.save_song('팀',song())
        with self.assertRaises(ValueError):await self.store.save_web_user('player1',{'role':'player'})
        # Admin login/diagnostic bootstrap is deliberately allowed.
        await self.store.save_web_user('admin',{'role':'admin'})
        self.assertEqual((await self.store.web_user('admin'))['role'],'admin')

    async def test_export_contains_all_categories_and_legacy_songs_alias(self):
        for c in SONG_CATEGORIES:await self.store.save_song('팀',song(category=c))
        data=await self.store.export();self.assertEqual(data['schema'],2)
        team=data['teams']['팀'];self.assertEqual(team['songs'],team['library']['entrance'])
        self.assertEqual(set(team['library']),set(SONG_CATEGORIES))

    async def test_invalid_category_cannot_form_arbitrary_firestore_path(self):
        for bad in ['webUsers','../users','admin','',None,{}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):await self.store.songs('팀',bad)

    def test_cheer_and_situation_never_become_voice_entry_song(self):
        c=song(category='cheer');s=song(category='situation');e=song()
        self.assertEqual(select_auto_song([c,s],{},MEMBER,'김선수',True),(None,None))
        selected,_=select_auto_song([c,e,s],{},MEMBER,'변한닉',False)
        self.assertIs(selected,e)


class LibraryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Settings(data_dir=Path(self.tmp.name),web_password=PASSWORD,web_only=True)
        await self.start()
        self.h=await self.login()
    async def start(self):
        self.store=Store(self.s);self.assets=Assets(self.s)
        self.bot=SimpleNamespace(settings=self.s,store=self.store,assets=self.assets,guilds=[],is_ready=lambda:False,get_guild=lambda _:None,player=SimpleNamespace(now={}))
        self.panel=WebPanel(self.bot);self.client=TestClient(TestServer(self.panel.app));await self.client.start_server()
    async def login(self,username='admin',password=PASSWORD):
        r=await self.client.post('/api/login',json={'username':username,'password':password,'authMode':'bearer'})
        self.assertEqual(r.status,200,await r.text());d=await r.json()
        return {'X-CSRF-Token':d['csrf'],'Authorization':'Bearer '+d['accessToken']}
    async def asyncTearDown(self):
        await self.client.close();await self.store.close();self.tmp.cleanup()
    async def upload(self):
        f=aiohttp.FormData();f.add_field('file',wav(),filename='우리 팀.wav',content_type='audio/wav')
        r=await self.client.post('/api/upload',data=f,headers=self.h);self.assertEqual(r.status,200,await r.text());return await r.json()
    async def save(self,**fields):
        return await self.client.post('/api/songs',json={'team':'팀',**song(),**fields},headers=self.h)

    async def test_real_audio_restart_and_relogin_preserves_three_libraries(self):
        asset=await self.upload();self.assertEqual(asset['sha256'],hashlib.sha256(wav()).hexdigest())
        for category in SONG_CATEGORIES:
            r=await self.save(category=category,source='upload',assetId=asset['id']);self.assertEqual(r.status,200,await r.text())
        oldtoken=self.h.copy()
        await self.client.close();await self.store.close();await self.start()
        r=await self.client.get('/api/state',headers=oldtoken);self.assertEqual(r.status,401)
        self.h=await self.login()
        r=await self.client.get('/api/state?team=팀',headers=self.h);data=await r.json()
        for rows in data['library'].values():
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['assetId'],asset['id']);self.assertFalse(rows[0].get('fileMissing',False))
        r=await self.client.get('/api/media/'+asset['id'],headers=self.h);self.assertEqual(await r.read(),wav())

    async def test_web_edit_changes_nickname_without_second_record(self):
        asset=await self.upload()
        self.assertEqual((await self.save(source='upload',assetId=asset['id'])).status,200)
        await self.store.save_lineup('팀',{'1':'김선수'})
        r=await self.save(name='새닉네임',oldName='김선수',source='upload',assetId=asset['id'])
        self.assertEqual(r.status,200,await r.text())
        rows=await self.store.songs('팀');self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['memberId'],MEMBER);self.assertEqual(rows[0]['assetId'],asset['id'])
        self.assertEqual((await self.store.lineup('팀'))['1'],'새닉네임')

    async def test_missing_audio_record_visible_and_can_rename_but_not_fabricate_upload(self):
        asset=await self.upload();await self.save(source='upload',assetId=asset['id'])
        self.assets.get(asset['id'])['path'].unlink()
        r=await self.client.get('/api/state?team=팀',headers=self.h);data=await r.json()
        self.assertTrue(data['library']['entrance'][0]['fileMissing'])
        r=await self.save(name='보존할닉',oldName='김선수',source='upload',assetId=asset['id'])
        self.assertEqual(r.status,200);self.assertIn('원본 오디오', (await r.json())['warning'])
        r=await self.save(name='없는파일',source='upload',assetId='a'*32);self.assertEqual(r.status,400)

    async def test_all_categories_play_through_correct_lookup(self):
        for category in SONG_CATEGORIES:await self.save(category=category)
        guild=SimpleNamespace(id=123,get_channel=lambda _:None)
        mockplay=AsyncMock(return_value=True)
        self.bot.is_ready=lambda:True;self.bot.get_guild=lambda _:guild
        self.bot.player=SimpleNamespace(play=mockplay,now={},voice=SimpleNamespace(status=lambda _:{'connected':True}))
        for category in SONG_CATEGORIES:
            r=await self.client.post('/api/control',headers=self.h,json={'action':'play','guildId':'123','team':'팀','name':'김선수','category':category})
            self.assertEqual(r.status,200,await r.text())
            self.assertEqual(mockplay.call_args.args[2]['category'],category)

    async def test_soundboard_link_only_accepts_registered_situation_song(self):
        await self.save(category='cheer')
        payload={'team':'팀','key':'homerun','songName':'김선수'}
        r=await self.client.put('/api/events',json=payload,headers=self.h);self.assertEqual(r.status,400)
        await self.save(category='situation')
        r=await self.client.put('/api/events',json=payload,headers=self.h);self.assertEqual(r.status,200)
        self.assertEqual((await self.store.event('팀','homerun'))['category'],'situation')

    async def test_backup_contains_actual_audio_metadata_not_accounts_or_secrets(self):
        asset=await self.upload();await self.save(source='upload',assetId=asset['id'])
        await self.store.save_song('팀',song('없는음원','cheer',source='upload',assetId='b'*32))
        r=await self.client.get('/api/backup',headers=self.h);self.assertEqual(r.status,200,await r.text() if r.status!=200 else '')
        with zipfile.ZipFile(io.BytesIO(await r.read())) as z:
            names=z.namelist()
            self.assertIn('uploads/'+asset['id']+'.wav',names)
            self.assertEqual(z.read('uploads/'+asset['id']+'.wav'),wav())
            data=json.loads(z.read('metadata.json'));self.assertEqual(data['schema'],2)
            info=json.loads(z.read('backup-info.json'));self.assertIn('b'*32,info['missingAssetIds'])
            for name in names:
                if name.endswith('.json'):
                    content=z.read(name).decode();self.assertNotIn('passwordHash',content);self.assertNotIn(PASSWORD,content)

    async def test_original_download_is_authenticated_and_filename_encoded(self):
        asset=await self.upload()
        r=await self.client.get('/api/media/'+asset['id']+'?download=1',headers=self.h)
        self.assertEqual(r.status,200);self.assertIn("attachment; filename*=UTF-8''",r.headers['Content-Disposition'])
        self.assertEqual(await r.read(),wav())
        r=await self.client.get('/api/media/'+asset['id']+'?download=1');self.assertEqual(r.status,401)
        r=await self.client.get('/api/backup');self.assertEqual(r.status,401)

    async def test_railway_without_volume_reports_warning_and_rejects_upload(self):
        self.assets.settings=replace(self.s,on_railway=True)
        self.panel.s=self.assets.settings
        r=await self.client.get('/api/state',headers=self.h)
        self.assertFalse((await r.json())['fileStorage']['uploadsAllowed'])
        form=aiohttp.FormData();form.add_field('file',wav(),filename='a.wav')
        r=await self.client.post('/api/upload',headers=self.h,data=form)
        self.assertEqual(r.status,400);self.assertIn('Volume',(await r.json())['error'])
        self.assertEqual(list(self.assets.root.iterdir()),[])

    async def test_player_can_register_and_rename_all_types_but_cannot_bind_or_delete_or_backup(self):
        r=await self.client.post('/api/signup',json={'username':'player1','displayName':'재생자','password':'user-password-1234'})
        self.assertEqual(r.status,201)
        r=await self.client.put('/api/users/player1',json={'role':'player'},headers=self.h);self.assertEqual(r.status,200)
        self.h=await self.login('player1','user-password-1234')
        for category in SONG_CATEGORIES:
            r=await self.save(category=category);self.assertEqual(r.status,200)
            r=await self.save(name='새닉',oldName='김선수',category=category);self.assertEqual(r.status,200,await r.text())
            r=await self.client.delete('/api/songs?team=팀&name=새닉&category='+category,headers=self.h);self.assertEqual(r.status,403)
        r=await self.client.put('/api/events',json={'team':'팀','key':'homerun','songName':'새닉'},headers=self.h);self.assertEqual(r.status,403)
        r=await self.client.get('/api/backup',headers=self.h);self.assertEqual(r.status,403)

    async def test_admin_can_create_rename_bind_and_delete_custom_game_situation(self):
        r=await self.client.post('/api/events',json={'team':'팀','label':'상대 투수 교체'},headers=self.h)
        self.assertEqual(r.status,201,await r.text());created=await r.json();key=created['key']
        doc=await self.store.event('팀',key);self.assertTrue(doc['isCustom']);self.assertEqual(doc['label'],'상대 투수 교체')
        r=await self.client.get('/api/state?team=팀',headers=self.h);data=await r.json()
        row=next(x for x in data['events'] if x['key']==key);self.assertTrue(row['customEvent']);self.assertEqual(row['label'],'상대 투수 교체')
        await self.save(name='교체 음악',category='situation')
        r=await self.client.put('/api/events',json={'team':'팀','key':key,'songName':'교체 음악'},headers=self.h);self.assertEqual(r.status,200,await r.text())
        r=await self.client.put('/api/events',json={'team':'팀','key':key,'label':'작전 타임'},headers=self.h);self.assertEqual(r.status,200,await r.text())
        doc=await self.store.event('팀',key);self.assertEqual(doc['label'],'작전 타임');self.assertEqual(doc['songName'],'교체 음악')
        r=await self.client.put('/api/events',json={'team':'팀','key':key,'assetId':None},headers=self.h);self.assertEqual(r.status,200)
        doc=await self.store.event('팀',key);self.assertEqual(doc['label'],'작전 타임');self.assertNotIn('songName',doc)
        r=await self.client.delete('/api/events?team=%ED%8C%80&key='+key,headers=self.h);self.assertEqual(r.status,200,await r.text())
        self.assertIsNone(await self.store.event('팀',key))

    async def test_custom_game_situation_duplicate_and_player_management_blocked(self):
        r=await self.client.post('/api/events',json={'team':'팀','label':'홈런'},headers=self.h);self.assertEqual(r.status,400)
        r=await self.client.post('/api/events',json={'team':'팀','label':'우리 상황'},headers=self.h);self.assertEqual(r.status,201);key=(await r.json())['key']
        await self.client.post('/api/signup',json={'username':'player3','displayName':'재생자','password':'user-password-1234'})
        await self.client.put('/api/users/player3',json={'role':'player'},headers=self.h)
        ph=await self.login('player3','user-password-1234')
        r=await self.client.post('/api/events',json={'team':'팀','label':'재생자 상황'},headers=ph);self.assertEqual(r.status,403)
        r=await self.client.put('/api/events',json={'team':'팀','key':key,'label':'변경'},headers=ph);self.assertEqual(r.status,403)
        r=await self.client.delete('/api/events?team=%ED%8C%80&key='+key,headers=ph);self.assertEqual(r.status,403)

    async def test_disabled_player_token_is_revoked(self):
        await self.client.post('/api/signup',json={'username':'player2','displayName':'재생자','password':'user-password-1234'})
        await self.client.put('/api/users/player2',json={'role':'player'},headers=self.h)
        playerheaders=await self.login('player2','user-password-1234')
        r=await self.client.put('/api/users/player2',json={'enabled':False},headers=self.h);self.assertEqual(r.status,200)
        r=await self.client.get('/api/state',headers=playerheaders);self.assertEqual(r.status,401)

if __name__=='__main__':unittest.main()
