"""Exact supplied domains and authentication regression tests (no cloud writes)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from config import ROOT, Settings, load_deployment_link
from storage import Store
from assets import Assets
from webapp import WebPanel

FRONT='https://appearance-song.web.app'
BACK='https://appeanrace-song-bot-production.up.railway.app'
ALIAS='https://appearance-song.firebaseapp.com'
PASSWORD='fixture-only-password-987'

class LinkedConfigurationTests(unittest.TestCase):
    def test_preconfigured_domains(self):
        d=load_deployment_link()
        self.assertEqual(d['api_url'], BACK)
        self.assertEqual(d['web_url'], FRONT)
        self.assertEqual(d['web_origins'], (FRONT, ALIAS))

    def test_legacy_wrong_url_variables_cannot_break_this_bundle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'DISCORD_TOKEN':'fixture-bot-token','WEB_ADMIN_PASSWORD':PASSWORD,'DATA_DIR':tmp,
            'PUBLIC_URL':FRONT,'WEB_URL':BACK,'WEB_ORIGINS':'*','WEB_ORIGIN':'http://wrong.test',
        }, clear=True):
            s=Settings.from_env()
            self.assertEqual(s.public_url, BACK)
            self.assertEqual(s.web_url, FRONT)
            self.assertEqual(s.web_origins, (FRONT, ALIAS))
            self.assertEqual(s.web_password, PASSWORD)
            self.assertEqual(s.token, 'fixture-bot-token')
            self.assertEqual(s.data_dir, Path(tmp))
            self.assertFalse(s.web_only)
            self.assertTrue(s.secure_cookie)

    def test_password_not_embedded_or_optional(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'WEB_ONLY':'true','DATA_DIR':tmp,
        }, clear=True):
            with self.assertRaisesRegex(ValueError, 'WEB_ADMIN_PASSWORD'):
                Settings.from_env()
        config=(ROOT/'static/config.js').read_text()
        self.assertNotIn(PASSWORD, config)
        self.assertNotIn('WEB_ADMIN_PASSWORD=',config)

    def test_short_password_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'WEB_ONLY':'true','DATA_DIR':tmp,'WEB_ADMIN_PASSWORD':'1234',
        }, clear=True):
            with self.assertRaisesRegex(ValueError, '12자'):
                Settings.from_env()

    def test_malformed_profile_fails_closed(self):
        for data in [{'api_url':BACK,'web_url':FRONT,'web_origins':['*']},
                     {'api_url':BACK,'web_url':FRONT,'web_origins':'*'},
                     {'api_url':FRONT,'web_url':FRONT},
                     {'api_url':'http://evil.test','web_url':FRONT},
                     {'api_url':BACK,'web_url':FRONT,'web_origins':['']},
                     []]:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'deployment-link.json').write_text(json.dumps(data))
                with patch('config.ROOT',root), self.assertRaises(ValueError):load_deployment_link()

    def test_website_copy_and_exact_csp(self):
        if not (ROOT/'웹사이트/firebase.json').is_file():
            self.skipTest('웹사이트용 ZIP도 웹사이트/ 폴더에 적용한 뒤 비교합니다.')
        for filename in ['index.html','config.js','app.js','style.css']:
            self.assertEqual((ROOT/'static'/filename).read_bytes(),(ROOT/'웹사이트/static'/filename).read_bytes())
        hosting=json.loads((ROOT/'웹사이트/firebase.json').read_text())['hosting']
        self.assertEqual(hosting['public'],'static')
        self.assertEqual(hosting['site'],'appearance-song')
        csp=next(h['value'] for r in hosting['headers'] for h in r['headers'] if h['key']=='Content-Security-Policy')
        self.assertIn("connect-src 'self' "+BACK+';',csp)
        self.assertNotIn('*.up.railway.app',csp)
        self.assertNotIn('id="railway-url"',(ROOT/'static/index.html').read_text())
        self.assertIn("sessionStorage.setItem(tokenKey(), token)",(ROOT/'static/app.js').read_text())

class LinkedAPIRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'WEB_ONLY':'true','WEB_ADMIN_PASSWORD':PASSWORD,'DATA_DIR':self.tmp.name},clear=True):
            self.s=Settings.from_env()
        self.store=Store(self.s)
        self.bot=SimpleNamespace(settings=self.s, store=self.store, assets=Assets(self.s), guilds=[],
            is_ready=lambda:False, get_guild=lambda _:None, player=SimpleNamespace(now={}))
        self.panel=WebPanel(self.bot)
        self.client=TestClient(TestServer(self.panel.app),cookie_jar=aiohttp.DummyCookieJar())
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();await self.store.close();self.tmp.cleanup()

    async def login(self, origin=FRONT):
        r=await self.client.post('/api/login',headers={'Origin':origin},json={'password':PASSWORD,'authMode':'bearer'})
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual(r.headers['Access-Control-Allow-Origin'], origin)
        self.assertNotIn('Set-Cookie',r.headers)
        return await r.json()

    async def test_frontend_preflight_login_and_authenticated_state(self):
        r=await self.client.options('/api/login',headers={'Origin':FRONT,
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
        self.assertEqual(r.status,204)
        d=await self.login()
        r=await self.client.get('/api/state',headers={'Origin':FRONT,'Authorization':'Bearer '+d['accessToken']})
        self.assertEqual(r.status,200)

    async def test_direct_railway_https_login_behind_http_proxy(self):
        # Reproduce the screenshot: TLS terminated upstream, app receives HTTP.
        r=await self.client.post('/api/login',headers={'Origin':BACK,'Host':BACK[8:]},
            json={'password':PASSWORD,'authMode':'bearer'})
        self.assertEqual(r.status,200,await r.text())
        token=(await r.json())['accessToken']
        r=await self.client.get('/api/state',headers={'Host':BACK[8:],'Authorization':'Bearer '+token})
        self.assertEqual(r.status,200,await r.text())

    async def test_alias_login(self):
        await self.login(ALIAS)

    async def test_all_management_endpoints_require_login(self):
        endpoints=[('GET','/api/session'),('POST','/api/logout'),('GET','/api/state'),
            ('POST','/api/team'),('POST','/api/songs'),('DELETE','/api/songs'),
            ('PUT','/api/lineup'),('POST','/api/upload'),('GET','/api/media/'+'a'*32),
            ('PUT','/api/events'),('POST','/api/control'),('GET','/api/diagnostics'),('GET','/api/export')]
        for method,path in endpoints:
            with self.subTest(path=path):
                r=await self.client.request(method,path,headers={'Origin':FRONT})
                self.assertEqual(r.status,401,await r.text())
                self.assertEqual(r.headers['Access-Control-Allow-Origin'],FRONT)

    async def test_untrusted_domains_and_originless_token_rejected(self):
        for origin in ['https://other.web.app','https://appearance-song.web.app.evil.test','null','https://other.up.railway.app']:
            with self.subTest(origin=origin):
                r=await self.client.post('/api/login',headers={'Origin':origin},
                    json={'password':PASSWORD,'authMode':'bearer'})
                self.assertEqual(r.status,403)
                self.assertNotIn('Access-Control-Allow-Origin',r.headers)
        d=await self.login()
        r=await self.client.get('/api/state',headers={'Authorization':'Bearer '+d['accessToken']})
        self.assertEqual(r.status,403)

    async def test_bad_password_has_readable_401_not_origin_error(self):
        r=await self.client.post('/api/login',headers={'Origin':FRONT},json={'password':'incorrect','authMode':'bearer'})
        self.assertEqual(r.status,401)
        self.assertIn('비밀번호', (await r.json())['error'])
        self.assertEqual(r.headers['Access-Control-Allow-Origin'],FRONT)

    async def test_connection_does_not_expose_secret_or_metadata(self):
        r=await self.client.get('/api/connection',headers={'Origin':FRONT})
        self.assertEqual(r.status,200)
        d=await r.json()
        self.assertEqual(d['build'],'library-20260919-1')
        for forbidden in ['accessToken','password','songs','guilds','firebase_key']:
            self.assertNotIn(forbidden,d)
        for path in ['/.env','/deployment-link.json','/config.py','/webapp.py']:
            r=await self.client.get(path)
            self.assertEqual(r.status,404)

if __name__=='__main__':unittest.main()
