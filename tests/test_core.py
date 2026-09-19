"""자격증명/외부 접속 없이 검증: python -m unittest discover -s tests -v"""
import asyncio
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import wave
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from assets import Assets
from config import Settings
from storage import Store
from validation import seconds, time_range, youtube_url, validate_song, clean_name, parse_song_args
from webapp import WebPanel, COOKIE

SONG = {'name':'김선수','url':'https://youtu.be/abcdefghijk?si=example','start':'0:10','end':'0:40','memberId':''}


def wav_bytes():
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b'\x00\x00' * 16000)
    return b.getvalue()


class ValidationTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(seconds('1:20'), 80)
        self.assertEqual(seconds('1:02:03.5'), 3723.5)
        self.assertEqual(seconds('30'), 30)
    def test_invalid_seconds(self):
        for t in ['nan','inf','-1','1:99','1:-20','1::2','abc','0.5:03']:
            with self.subTest(t=t), self.assertRaises(ValueError): seconds(t)
    def test_ranges(self):
        for a,b in [(10,10),(30,20),(0,3601),('x',30)]:
            with self.subTest(a=a), self.assertRaises(ValueError): time_range(a,b)
    def test_url(self):
        self.assertEqual(youtube_url(SONG['url']), 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertEqual(youtube_url('https://www.youtube.com/watch?list=x&v=abcdefghijk&t=3'), 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertEqual(youtube_url('https://youtube.com/shorts/abcdefghijk'), 'https://www.youtube.com/watch?v=abcdefghijk')
    def test_ssrf_rejected(self):
        for u in ['file:///etc/passwd','http://127.0.0.1/music.mp3','http://169.254.169.254/','https://youtube.com.attacker.test/watch?v=abcdefghijk','https://attacker@youtube.com/watch?v=abcdefghijk','javascript:alert(1)','https://youtube.com:99/watch?v=abcdefghijk','https://youtube.com/watch?v=no']:
            with self.subTest(url=u), self.assertRaises(ValueError): youtube_url(u)
    def test_name(self):
        for n in ['','a/b','..','__reserved__','x\ny','x\\y']:
            with self.subTest(name=n), self.assertRaises(ValueError): clean_name(n)
        self.assertEqual(clean_name('김 선수'), '김 선수')
    def test_legacy_command(self):
        d=parse_song_args('김선수 / https://youtu.be/abcdefghijk / 0:10~0:40')
        self.assertEqual((d['start'],d['end']),(10,40))
    def test_asset_id(self):
        with self.assertRaises(ValueError): validate_song({**SONG,'source':'upload','assetId':'../../etc/passwd'})
    def test_member_id(self):
        with self.assertRaises(ValueError): validate_song({**SONG,'memberId':'x'})
        self.assertEqual(validate_song({**SONG,'memberId':'123456789012345678'})['memberId'], '123456789012345678')


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Settings(data_dir=Path(self.tmp.name))
        self.store=Store(self.s)
    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()
    async def test_crud(self):
        await self.store.save_song('A팀',validate_song(SONG))
        self.assertEqual((await self.store.song('A팀','김선수'))['start'],10)
        await self.store.save_song('A팀',validate_song({**SONG,'start':12}))
        self.assertEqual(len(await self.store.songs('A팀')),1)
        self.assertEqual((await self.store.song('A팀','김선수'))['start'],12)
    async def test_legacy_schema_without_parent(self):
        await self.store._run(self.store._write,'teams/옛팀/entranceSongs/기존선수',{'url':SONG['url'],'start':0,'end':20})
        self.assertIn('옛팀',await self.store.teams())
        self.assertEqual((await self.store.song('옛팀','기존선수'))['source'],'youtube')
    async def test_rename_and_delete_lineup(self):
        await self.store.save_song('A팀',validate_song(SONG))
        await self.store.save_lineup('A팀',{'1':'김선수','9':'김선수'})
        await self.store.rename('A팀','김선수','새선수')
        self.assertIsNone(await self.store.song('A팀','김선수'))
        self.assertEqual((await self.store.lineup('A팀'))['9'],'새선수')
        await self.store.delete_song('A팀','새선수')
        self.assertEqual((await self.store.lineup('A팀'))['1'],'')
    async def test_duplicate_rename(self):
        for n in ['김선수','새선수']: await self.store.save_song('A팀',validate_song({**SONG,'name':n}))
        with self.assertRaises(ValueError): await self.store.rename('A팀','김선수','새선수')
        self.assertEqual(len(await self.store.songs('A팀')),2)
    async def test_sql_wildcards_do_not_leak(self):
        await self.store.save_song('팀_%',validate_song(SONG))
        await self.store.save_song('팀_다른팀',validate_song({**SONG,'name':'다른선수'}))
        self.assertEqual([s['name'] for s in await self.store.songs('팀_%')],['김선수'])
    async def test_state_and_team_persist(self):
        await self.store.set_team(123,'B팀')
        await self.store.set_volume('B팀',.7)
        await self.store.set_order('B팀',9)
        await self.store.close(); self.store=Store(self.s)
        self.assertEqual(await self.store.get_team(123),'B팀')
        self.assertEqual(await self.store.state('B팀'),{'volume':.7,'currentOrder':9})
    async def test_concurrent_writes(self):
        await asyncio.gather(*(self.store.save_song('팀',validate_song({**SONG,'name':f'선수{i}'})) for i in range(30)))
        self.assertEqual(len(await self.store.songs('팀')),30)
    async def test_events_export(self):
        await self.store.set_event('A팀','out',{'file':'out.mp3'})
        d=await self.store.export()
        self.assertEqual(d['teams']['A팀']['events'][0]['file'],'out.mp3')
    async def test_lineup_range(self):
        with self.assertRaises(ValueError): await self.store.save_lineup('A팀',{'10':'김선수'})


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Settings(data_dir=Path(self.tmp.name),web_password='Test-password-123456',web_only=True)
        self.store=Store(self.s); self.assets=Assets(self.s)
        self.bot=SimpleNamespace(settings=self.s,store=self.store,assets=self.assets,guilds=[],is_ready=lambda:False,get_guild=lambda _:None,player=SimpleNamespace(now={}))
        self.panel=WebPanel(self.bot)
        self.client=TestClient(TestServer(self.panel.app))
        await self.client.start_server()
        self.csrf=''
    async def asyncTearDown(self):
        await self.client.close(); await self.store.close(); self.tmp.cleanup()
    async def login(self):
        r=await self.client.post('/api/login',json={'password':self.s.web_password})
        self.assertEqual(r.status,200)
        self.csrf=(await r.json())['csrf']
        return r
    @property
    def headers(self): return {'X-CSRF-Token':self.csrf}
    async def test_auth_required(self):
        for path in ['/api/state','/api/export','/api/diagnostics','/api/media/'+'a'*32]:
            r=await self.client.get(path); self.assertEqual(r.status,401)
    async def test_login_cookie(self):
        r=await self.login()
        cookie=r.cookies[COOKIE]
        self.assertTrue(cookie['httponly']);self.assertEqual(cookie['samesite'],'Strict')
        r=await self.client.get('/api/session');self.assertEqual((await r.json())['csrf'],self.csrf)
    async def test_password_wrong(self):
        r=await self.client.post('/api/login',json={'password':'wrong'}); self.assertEqual(r.status,401)
    async def test_rate_limit(self):
        for _ in range(10): await self.client.post('/api/login',json={'password':'wrong'})
        r=await self.client.post('/api/login',json={'password':'wrong'});self.assertEqual(r.status,429)
    async def test_csrf(self):
        await self.login()
        r=await self.client.post('/api/team',json={'team':'팀'});self.assertEqual(r.status,403)
        r=await self.client.post('/api/team',json={'team':'팀'},headers={**self.headers,'Origin':'https://attacker.test'});self.assertEqual(r.status,403)
        r=await self.client.post('/api/team',json={'team':'팀'},headers=self.headers);self.assertEqual(r.status,200)
    async def test_flow(self):
        await self.login()
        r=await self.client.post('/api/songs',json={'team':'테스트팀',**SONG},headers=self.headers); self.assertEqual(r.status,200,await r.text())
        r=await self.client.put('/api/lineup',json={'team':'테스트팀','lineup':{'1':'김선수'}},headers=self.headers);self.assertEqual(r.status,200)
        r=await self.client.get('/api/state?team=테스트팀');d=await r.json()
        self.assertEqual(d['songs'][0]['name'],'김선수');self.assertEqual(d['lineup']['1'],'김선수')
        r=await self.client.delete('/api/songs?team=테스트팀&name=김선수',headers=self.headers);self.assertEqual(r.status,200)
        self.assertEqual((await self.store.lineup('테스트팀'))['1'],'')
    async def test_invalid_song(self):
        await self.login()
        for url in ['http://127.0.0.1/x','file:///etc/passwd']:
            r=await self.client.post('/api/songs',json={**SONG,'team':'A팀','url':url},headers=self.headers);self.assertEqual(r.status,400)
    async def test_audio_upload_and_range(self):
        await self.login()
        form=aiohttp.FormData();form.add_field('file',wav_bytes(),filename='김선수.wav',content_type='audio/wav')
        r=await self.client.post('/api/upload',data=form,headers=self.headers);self.assertEqual(r.status,200,await r.text())
        d=await r.json();self.assertAlmostEqual(d['duration'],2,places=1)
        song={**SONG,'team':'A팀','source':'upload','assetId':d['id'],'start':0,'end':1.5}
        r=await self.client.post('/api/songs',json=song,headers=self.headers);self.assertEqual(r.status,200)
        r=await self.client.get('/api/media/'+d['id'],headers={'Range':'bytes=0-99'});self.assertEqual(r.status,206);self.assertEqual(len(await r.read()),100)
        r=await self.client.post('/api/songs',json={**song,'end':10},headers=self.headers);self.assertEqual(r.status,400)
    async def test_fake_audio_rejected(self):
        await self.login()
        for name,data in [('test.exe',b'no'),('test.mp3',b'not audio')]:
            form=aiohttp.FormData();form.add_field('file',data,filename=name)
            r=await self.client.post('/api/upload',data=form,headers=self.headers);self.assertEqual(r.status,400)
        self.assertEqual(list(self.assets.root.iterdir()),[])
    async def test_logout(self):
        await self.login();r=await self.client.post('/api/logout',json={},headers=self.headers);self.assertEqual(r.status,200)
        r=await self.client.get('/api/state');self.assertEqual(r.status,401)
    async def test_security_headers_and_secret_not_exposed(self):
        await self.login();r=await self.client.get('/api/diagnostics');t=await r.text()
        self.assertNotIn(self.s.web_password,t);self.assertNotIn('DISCORD_TOKEN=',t)
        self.assertEqual(r.headers['X-Frame-Options'],'DENY');self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
    async def test_static(self):
        for path in ['/','/assets/app.js','/assets/style.css','/healthz']:
            r=await self.client.get(path);self.assertEqual(r.status,200)
        r=await self.client.get('/assets/config.py');self.assertEqual(r.status,404)
    async def test_streamed_json_request(self):
        await self.login()
        async def chunks():
            yield b'{"team":'
            await asyncio.sleep(.01)
            yield '"분할요청"}'.encode()
        r=await self.client.post('/api/team',data=chunks(),headers={**self.headers,'Content-Type':'application/json'})
        self.assertEqual(r.status,200,await r.text())
        self.assertIn('분할요청',await self.store.teams())
    async def test_oversize_json_rejected(self):
        await self.login()
        r=await self.client.post('/api/team',json={'team':'x'*70000},headers=self.headers)
        self.assertEqual(r.status,400)
    async def test_offline_controls_fail_clearly(self):
        await self.login();r=await self.client.post('/api/control',json={'guildId':'123','action':'connect'},headers=self.headers)
        self.assertEqual(r.status,400);self.assertIn('연결되어 있지',await r.text())


if __name__=='__main__':unittest.main()
