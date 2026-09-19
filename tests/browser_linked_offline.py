"""Browser regression with supplied HTTPS origins, intercepted network, real local API.

All public-host requests are intercepted and fulfilled locally: this is NOT a cloud
integration test. No DNS lookup, real Discord request or cloud write is performed.
Because interception can change browser CORS preflight behavior, use test_linked.py
and test_split.py to test preflight handling explicitly.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from config import Settings, ROOT
from storage import Store
from assets import Assets
from webapp import WebPanel
from browser_split_smoke import MockPlayer, wav_bytes
FRONT='https://appearance-song.web.app'
BACK='https://appeanrace-song-bot-production.up.railway.app'
PASSWORD='browser-fixture-password-123'

async def main():
    output=Path(os.getenv('TEST_OUTPUT_DIR',str(ROOT/'test-output')));output.mkdir(parents=True,exist_ok=True)
    passed=[];errors=[];seen=[]
    with tempfile.TemporaryDirectory() as temp:
        s=Settings(data_dir=Path(temp),web_password=PASSWORD,public_url=BACK,web_url=FRONT,web_origins=(FRONT,),secure_cookie=True)
        store=Store(s);assets=Assets(s);player=MockPlayer()
        ch=SimpleNamespace(id=200,name='테스트 통화방',members=[],permissions_for=lambda m:SimpleNamespace(view_channel=True,connect=True,speak=True))
        guild=SimpleNamespace(id=100,name='로컬 테스트 서버',me=object(),voice_channels=[ch],get_channel=lambda n:ch if n==200 else None)
        bot=SimpleNamespace(settings=s,store=store,assets=assets,player=player,guilds=[guild],is_ready=lambda:True,get_guild=lambda n:guild if n==100 else None)
        panel=WebPanel(bot)
        client=TestClient(TestServer(panel.app),cookie_jar=aiohttp.DummyCookieJar());await client.start_server()
        async def route_handler(route):
            req=route.request;u=urlsplit(req.url);origin=f'{u.scheme}://{u.netloc}'
            if origin==FRONT:
                filename='index.html' if u.path=='/' else u.path.lstrip('/')
                if filename not in {'index.html','config.js','app.js','style.css'}:
                    await route.fulfill(status=404,body='Not found');return
                mime='text/css' if filename.endswith('.css') else 'text/javascript' if filename.endswith('.js') else 'text/html'
                manifest=json.loads((ROOT/'웹사이트/firebase.json').read_text())
                headers={h['key']:h['value'] for block in manifest['hosting']['headers'] for h in block['headers']}
                headers['Content-Type']=mime+'; charset=utf-8'
                await route.fulfill(status=200,headers=headers,body=(ROOT/'웹사이트/static'/filename).read_bytes())
            elif origin==BACK:
                h=await req.all_headers()
                h={k:v for k,v in h.items() if k.lower() not in {'content-length','connection','accept-encoding'}}
                h['Host']=u.netloc
                path=u.path+('?' + u.query if u.query else '')
                seen.append({'method':req.method,'path':u.path,'origin':h.get('origin'),'auth':bool(h.get('authorization'))})
                r=await client.request(req.method,path,headers=h,data=req.post_data_buffer,allow_redirects=False)
                data=await r.read()
                headers={k:v for k,v in r.headers.items() if k.lower() not in {'content-length','transfer-encoding','content-encoding','connection'}}
                await route.fulfill(status=r.status,headers=headers,body=data)
            else:
                await route.abort()
        try:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
                context=await browser.new_context(viewport={'width':1366,'height':960},accept_downloads=True)
                await context.route('**/*',route_handler)
                page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                # A stale per-browser address must never override the preconfigured API.
                await context.add_init_script("localStorage.setItem('appearance:railway-api:v1:'+location.origin,'https://wrong.up.railway.app');")
                await page.goto(FRONT)
                await page.wait_for_function("document.querySelector('#connection-status').textContent.includes('웹 연결 정상')")
                assert await page.locator('#server-address').text_content()==BACK
                assert await page.locator('#app').is_hidden()
                assert await page.locator('#railway-url').count()==0
                assert not any(x['path']=='/api/state' for x in seen)
                passed.append('supplied domains configured; stale saved URL ignored; login gate does not fetch private state')
                await page.screenshot(path=str(output/'appearance-linked-login.png'),full_page=True)
                await page.fill('#password','wrong-long-password');await page.click('#login-form button[type=submit]')
                await page.wait_for_function("document.querySelector('#login-error').textContent.includes('비밀번호')")
                assert await page.locator('#app').is_hidden();passed.append('wrong password rejected without exposing management view')
                await page.fill('#password',PASSWORD);await page.click('#login-form button[type=submit]')
                await page.locator('#app').wait_for(state='visible')
                await page.wait_for_function("document.querySelector('#guild-select').value==='100'")
                assert await context.cookies()==[]
                passed.append('cross-origin bearer login loads local API state with no cookies')
                await page.select_option('#voice-select','200');await page.click('#connect')
                await page.wait_for_function("document.querySelector('#voice-title').textContent.includes('연결됨')")
                assert player.last=='connect';passed.append('connection button reaches mocked voice manager')
                await page.fill('#song-name','웹등록선수');await page.fill('#song-url','https://youtu.be/abcdefghijk');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#song-count').textContent==='1'")
                assert await store.song('A팀','웹등록선수') is not None
                passed.append('song saved through API into local SQLite')
                await page.click('#source-upload');await page.set_input_files('#song-file',{'name':'테스트.wav','mimeType':'audio/wav','buffer':wav_bytes()})
                await page.wait_for_function("document.querySelector('#upload-preview').readyState>=1 && !document.querySelector('#save-song').disabled")
                assert await page.locator('#upload-preview').evaluate('(a)=>a.src.startsWith("blob:")')
                passed.append('audio upload passes ffprobe and authenticated preview uses a blob URL')
                await page.fill('#song-name','업로드선수');await page.fill('#song-end','0:01');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#song-count').textContent==='2'")
                row=page.locator('.song-row').filter(has=page.locator('h4',has_text='업로드선수'))
                await row.get_by_role('button',name='▶ 재생',exact=True).click()
                assert player.last==('play','업로드선수');passed.append('playback reaches mock player')
                await page.click('.nav[data-tab=lineup]');await page.select_option('#lineup-grid select[data-order="1"]','웹등록선수');await page.click('#save-lineup')
                await page.wait_for_timeout(200)
                assert (await store.lineup('A팀'))['1']=='웹등록선수';passed.append('lineup save')
                await page.click('.nav[data-tab=diagnostics]')
                async with page.expect_download() as download:await page.click('#export-backup')
                data=json.loads(Path(await (await download.value).path()).read_text())
                assert 'A팀' in data['teams'];passed.append('authenticated metadata backup download')
                await page.reload();await page.locator('#app').wait_for(state='visible')
                await page.wait_for_function("document.querySelector('#song-count').textContent==='2'")
                passed.append('page reload restores the current tab session')
                await page.screenshot(path=str(output/'appearance-linked-desktop.png'),full_page=True)
                for width in [360,390,768]:
                    await page.set_viewport_size({'width':width,'height':844})
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                await page.screenshot(path=str(output/'appearance-linked-mobile.png'),full_page=True)
                passed.append('360/390/768px management screens have no horizontal overflow')
                await page.set_viewport_size({'width':390,'height':844})
                await page.click('#logout-mobile');await page.locator('#login-screen').wait_for(state='visible')
                await page.reload();await page.wait_for_function("document.querySelector('#connection-status').textContent.includes('웹 연결 정상')")
                assert await page.locator('#app').is_hidden()
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                await page.screenshot(path=str(output/'appearance-linked-login-mobile.png'),full_page=True)
                passed.append('logout stays logged out on reload; 390px login has no overflow')
                same=await context.new_page();same.on('pageerror',lambda e:errors.append(str(e)))
                await same.goto(BACK);await same.fill('#password',PASSWORD);await same.click('#login-form button[type=submit]')
                await same.locator('#app').wait_for(state='visible')
                await same.wait_for_function("document.querySelector('#song-count').textContent==='2'")
                passed.append('Railway-origin panel login also works with HTTPS-to-HTTP proxy simulation')
                assert not errors,errors
                passed.append('no uncaught JavaScript errors')
                await browser.close()
        finally:
            await client.close();await store.close()
    result={'count':len(passed),'passed':passed,'pageErrors':errors,'network':'intercepted HTTPS requests forwarded into real local aiohttp API; not live cloud',
        'voice':'mocked; no actual Discord connection','preflight':'covered separately by API unit tests; browser interception may alter preflight behavior'}
    (output/'browser-linked-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':asyncio.run(main())
