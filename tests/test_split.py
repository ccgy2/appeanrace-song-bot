"""Split-host API tests, without cloud accounts or a real Discord connection."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from assets import Assets
from config import Settings, normalize_origin, ROOT
try:
    from setup_deploy import configure, origin
except ModuleNotFoundError:
    configure = origin = None  # Legacy helper absent from the user's supplied ZIP.
from storage import Store
from webapp import WebPanel, COOKIE
from test_core import wav_bytes, SONG

FRONT = 'https://studio-test.web.app'
ALIAS = 'https://studio-test.firebaseapp.com'
BACK = 'https://bot-test.up.railway.app'

class OriginTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_origin('https://EXAMPLE.com:443/'), 'https://example.com')
        self.assertEqual(normalize_origin('http://localhost:8090/'), 'http://localhost:8090')
        self.assertEqual(normalize_origin('https://example.com:8090'), 'https://example.com:8090')
    def test_invalid_origins(self):
        for value in ['*','null','https://*.web.app','https://x.test/path','http://public.test',
                      'https://user:pass@x.test','https://x.test?q=1','https://x.test#x',
                      'https://x.test:bad','https://x.test\\evil','https://x.test\r\nX-Test:1']:
            with self.subTest(value=value), self.assertRaises(ValueError): normalize_origin(value)
    def test_settings_alias(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'WEB_ONLY':'true','WEB_ADMIN_PASSWORD':'unit-test-long-password','DATA_DIR':tmp,
            'PUBLIC_URL':BACK,'WEB_URL':FRONT,'WEB_ORIGIN':ALIAS,'WEB_ORIGINS':FRONT+','+FRONT,
        }, clear=True), patch('config.load_deployment_link', return_value={}):
            s=Settings.from_env()
            self.assertEqual(s.web_origins,(FRONT,ALIAS))
            self.assertEqual(s.web_url,FRONT)
            self.assertEqual(s.public_url,BACK)
    @unittest.skipIf(origin is None, 'Original ZIP does not contain legacy setup_deploy.py')
    def test_deploy_https_only(self):
        for value in ['http://localhost:8080','https://host.railway.internal','https://a.test/path','https://localhost','https://127.0.0.1']:
            with self.subTest(value=value), self.assertRaises(ValueError): origin(value)

class SplitWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Settings(data_dir=Path(self.tmp.name),web_password='test-only-password-123',web_only=True,
                        web_origins=(FRONT,ALIAS),web_url=FRONT,public_url=BACK)
        self.store=Store(self.s)
        self.bot=SimpleNamespace(settings=self.s,store=self.store,assets=Assets(self.s),guilds=[],
                                 is_ready=lambda:False,get_guild=lambda _:None,player=SimpleNamespace(now={}))
        self.panel=WebPanel(self.bot)
        self.client=TestClient(TestServer(self.panel.app),cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()
        self.token=''; self.csrf=''
    async def asyncTearDown(self):
        await self.client.close(); await self.store.close(); self.tmp.cleanup()
    async def login(self, front=FRONT):
        r=await self.client.post('/api/login',json={'password':self.s.web_password,'authMode':'bearer'},headers={'Origin':front})
        self.assertEqual(r.status,200,await r.text())
        d=await r.json(); self.token=d['accessToken'];self.csrf=d['csrf']
        return r,d
    @property
    def headers(self):
        return {'Origin':FRONT,'Authorization':'Bearer '+self.token,'X-CSRF-Token':self.csrf}
    async def test_connection_without_auth(self):
        r=await self.client.get('/api/connection',headers={'Origin':FRONT});d=await r.json()
        self.assertEqual(d['apiVersion'],2);self.assertEqual(d['service'],'appearance-song-bot')
        self.assertEqual(r.headers['Access-Control-Allow-Origin'],FRONT)
        self.assertNotIn(self.s.web_password,await r.text())
    async def test_bearer_no_cookie(self):
        r,d=await self.login()
        self.assertNotIn('Set-Cookie',r.headers)
        self.assertGreater(len(self.token),40)
        self.assertEqual(d['expiresIn'],28800)
        r=await self.client.get('/api/session',headers=self.headers)
        self.assertEqual(r.status,200);self.assertEqual((await r.json())['csrf'],self.csrf)
    async def test_preflight(self):
        for method,headers in [('POST','authorization,content-type,x-csrf-token'),('PUT','authorization,x-csrf-token'),('DELETE','authorization,x-csrf-token'),('GET','authorization,range')]:
            r=await self.client.options('/api/songs',headers={'Origin':FRONT,'Access-Control-Request-Method':method,'Access-Control-Request-Headers':headers})
            self.assertEqual(r.status,204)
            self.assertEqual(r.headers['Access-Control-Allow-Origin'],FRONT)
            self.assertNotIn('Access-Control-Allow-Credentials',r.headers)
    async def test_preflight_reject_headers_methods(self):
        for method,headers in [('PATCH','authorization'),('POST','x-admin-override')]:
            r=await self.client.options('/api/songs',headers={'Origin':FRONT,'Access-Control-Request-Method':method,'Access-Control-Request-Headers':headers})
            self.assertEqual(r.status,403)
    async def test_untrusted_origins_blocked(self):
        for front in ['https://evil.test','https://studio-test.web.app.evil.test','null','https://evil.web.app']:
            r=await self.client.post('/api/login',headers={'Origin':front},json={'password':self.s.web_password,'authMode':'bearer'})
            self.assertEqual(r.status,403);self.assertNotIn('Access-Control-Allow-Origin',r.headers)
    async def test_unauthorized_readable(self):
        r=await self.client.get('/api/state',headers={'Origin':FRONT})
        self.assertEqual(r.status,401);self.assertEqual(r.headers['Access-Control-Allow-Origin'],FRONT)
    async def test_bearer_csrf_enforced(self):
        await self.login()
        h={'Origin':FRONT,'Authorization':'Bearer '+self.token}
        r=await self.client.post('/api/team',headers=h,json={'team':'팀'})
        self.assertEqual(r.status,403)
        r=await self.client.post('/api/team',headers=self.headers,json={'team':'팀'})
        self.assertEqual(r.status,200)
    async def test_origin_binding(self):
        await self.login()
        r=await self.client.get('/api/state',headers={**self.headers,'Origin':ALIAS})
        self.assertEqual(r.status,403)
    async def test_same_origin_get_without_origin(self):
        await self.login(BACK)
        r=await self.client.get('/api/state',headers={'Authorization':'Bearer '+self.token})
        self.assertEqual(r.status,200)
    async def test_query_token_not_supported(self):
        await self.login()
        r=await self.client.get('/api/state?token='+self.token,headers={'Origin':FRONT})
        self.assertEqual(r.status,401)
    async def test_cookie_cannot_impersonate_bearer(self):
        await self.login()
        r=await self.client.get('/api/state',headers={'Origin':FRONT,'Cookie':COOKIE+'='+self.token})
        self.assertEqual(r.status,401)
    async def test_expiry(self):
        await self.login();self.panel.sessions[self.token]['expires']=time.monotonic()-1
        r=await self.client.get('/api/state',headers=self.headers)
        self.assertEqual(r.status,401)
    async def test_logout_revokes_token(self):
        await self.login()
        r=await self.client.post('/api/logout',headers=self.headers,json={});self.assertEqual(r.status,200)
        r=await self.client.get('/api/state',headers=self.headers);self.assertEqual(r.status,401)
    async def test_cross_origin_song_lineup_export_delete(self):
        await self.login()
        r=await self.client.post('/api/songs',headers=self.headers,json={**SONG,'team':'웹팀'});self.assertEqual(r.status,200)
        r=await self.client.put('/api/lineup',headers=self.headers,json={'team':'웹팀','lineup':{'1':SONG['name']}});self.assertEqual(r.status,200)
        r=await self.client.get('/api/state?team=웹팀',headers=self.headers)
        d=await r.json();self.assertEqual(d['songs'][0]['name'],SONG['name']);self.assertEqual(d['lineup']['1'],SONG['name'])
        r=await self.client.get('/api/export',headers=self.headers)
        self.assertIn('웹팀',(await r.json())['teams']);self.assertIn('Content-Disposition',r.headers['Access-Control-Expose-Headers'])
        r=await self.client.delete('/api/songs?team=웹팀&name='+SONG['name'],headers=self.headers);self.assertEqual(r.status,200)
    async def test_cross_origin_upload_media_range(self):
        await self.login()
        f=aiohttp.FormData();f.add_field('file',wav_bytes(),filename='선수.wav',content_type='audio/wav')
        r=await self.client.post('/api/upload',headers=self.headers,data=f);self.assertEqual(r.status,200)
        asset=(await r.json())['id']
        r=await self.client.get('/api/media/'+asset,headers={**self.headers,'Range':'bytes=0-99'})
        self.assertEqual(r.status,206);self.assertEqual(len(await r.read()),100)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'],FRONT)
        r=await self.client.get('/api/media/'+asset,headers={'Origin':FRONT});self.assertEqual(r.status,401)
    async def test_discord_control_dispatch_mock(self):
        # Actual HTTP request -> actual control handler -> mocked voice connector.
        channel=SimpleNamespace(id=200)
        guild=SimpleNamespace(id=100,get_channel=lambda x:channel if x==200 else None)
        connect=AsyncMock()
        self.bot.is_ready=lambda:True;self.bot.get_guild=lambda x:guild if x==100 else None
        self.bot.player=SimpleNamespace(voice=SimpleNamespace(connect=connect,status=lambda g:{'connected':True}),now={})
        await self.login()
        r=await self.client.post('/api/control',headers=self.headers,json={'action':'connect','guildId':'100','channelId':'200','team':'A팀'})
        self.assertEqual(r.status,200,await r.text());connect.assert_awaited_once_with(guild,channel)
    async def test_new_static_paths(self):
        for path in ['/','/app.js','/style.css','/config.js','/assets/app.js']:
            r=await self.client.get(path);self.assertEqual(r.status,200,path)
        r=await self.client.get('/.env');self.assertEqual(r.status,404)

@unittest.skipIf(configure is None, 'Original ZIP does not contain legacy setup_deploy.py; actual Firebase config is tested separately')
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        shutil.copytree(ROOT/'static',self.root/'static')
        shutil.copytree(ROOT/'scripts',self.root/'scripts')
        shutil.copy2(ROOT/'firebase.json',self.root/'firebase.json')
    def tearDown(self): self.tmp.cleanup()
    def test_generated_config(self):
        result=configure(self.root,'my-bot-project',FRONT,BACK)
        self.assertEqual(result['site'],'studio-test')
        self.assertEqual(result['variables']['WEB_ORIGINS'],FRONT+','+ALIAS)
        manifest=json.loads((self.root/'firebase.json').read_text())
        self.assertEqual(manifest['hosting']['public'],'static');self.assertEqual(manifest['hosting']['site'],'studio-test')
        self.assertNotIn('DISCORD_TOKEN',(self.root/'railway-web-variables.txt').read_text())
        self.assertEqual(json.loads((self.root/'.firebaserc').read_text())['projects']['default'],'my-bot-project')
        p=subprocess.run(['node',str(self.root/'scripts/check-hosting.cjs')],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
    def test_predeploy_allows_runtime_railway_config(self):
        firebase=json.loads((self.root/'firebase.json').read_text())
        firebase['hosting']['site']='appearance-song'
        for block in firebase['hosting']['headers']:
            for h in block.get('headers',[]):
                if h.get('key','').lower()=='content-security-policy':
                    h['value']=h['value'].replace("connect-src 'self'", "connect-src 'self' https://*.up.railway.app")
        (self.root/'firebase.json').write_text(json.dumps(firebase))
        p=subprocess.run(['node',str(self.root/'scripts/check-hosting.cjs')],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
    def test_custom_domain(self):
        r=configure(self.root,'my-bot-project','https://studio.example.com',BACK,'studio-test')
        self.assertEqual(r['variables']['WEB_ORIGINS'],'https://studio.example.com,'+FRONT+','+ALIAS)
    def test_mismatched_site_rejected(self):
        with self.assertRaises(ValueError):configure(self.root,'my-bot-project',FRONT,BACK,'another-site')
    def test_preview_rejected(self):
        with self.assertRaises(ValueError):configure(self.root,'my-bot-project','https://studio-test--preview-abcd.web.app',BACK)
    def test_backups_existing_configuration(self):
        configure(self.root,'my-bot-project',FRONT,BACK)
        configure(self.root,'my-bot-project',FRONT,'https://second-bot.up.railway.app')
        backups=list((self.root/'.deploy-backups').rglob('config.js'))
        self.assertTrue(any(BACK in f.read_text() for f in backups))
    def test_predeploy_blocks_unexpected_public_file(self):
        configure(self.root,'my-bot-project',FRONT,BACK)
        (self.root/'static'/'accidental-secret.txt').write_text('fake secret')
        p=subprocess.run(['node',str(self.root/'scripts/check-hosting.cjs')],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0)

if __name__=='__main__':unittest.main()
