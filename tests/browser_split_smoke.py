"""Optional browser smoke test using TWO LOCAL ORIGINS, a real HTTP API and SQLite.
Discord voice actions are mock objects. No real cloud account or bot token is used.
Requires playwright and Chromium; use CHROMIUM_PATH for a system executable.
Run: python tests/browser_split_smoke.py
"""
import asyncio
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import wave
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from playwright.async_api import async_playwright
from config import Settings, ROOT
from storage import Store
from assets import Assets
from webapp import WebPanel


def wav_bytes():
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*16000)
    return b.getvalue()


class MockPlayer:
    def __init__(self):
        self.connected=False;self.now={};self.last=None
        self.voice=SimpleNamespace(connect=self.connect,status=self.status)
    def status(self,g):
        return {'connected':self.connected,'channelId':'200' if self.connected else None,
                'channelName':'테스트 통화방' if self.connected else None,'latencyMs':12,
                'serverMuted':False,'error':None}
    async def connect(self,g,c): self.connected=True;self.last='connect'
    async def leave(self,g): self.connected=False;self.last='disconnect'
    async def play(self,g,t,song,**kw):
        self.last=('play',song['name']);self.now[g.id]={'title':song['name'],'status':'playing'}
    def stop(self,g): self.last='stop';self.now[g.id]={'status':'stopped'}
    async def volume(self,*a): self.last='volume'
    async def event(self,*a): self.last='event'


async def main():
    output=Path(os.getenv('TEST_OUTPUT_DIR',str(ROOT/'test-output')));output.mkdir(parents=True,exist_ok=True)
    passed=[];errors=[]; requests=[]; dynamic={}
    with tempfile.TemporaryDirectory() as tmp:
        async def frontend(request):
            if request.path == '/config.js':
                return web.Response(text='window.APPEARANCE_CONFIG = Object.freeze('+json.dumps({'API_BASE_URL':dynamic['api']})+');',content_type='text/javascript')
            name='index.html' if request.path=='/' else request.path[1:]
            if name not in {'index.html','app.js','style.css'}:raise web.HTTPNotFound()
            resp=web.FileResponse(ROOT/'static'/name)
            resp.headers['Content-Security-Policy']=("default-src 'self'; script-src 'self'; style-src 'self'; "
                f"connect-src 'self' {dynamic['api']}; media-src 'self' blob:; img-src 'self' data:; object-src 'none'")
            return resp
        frontend_app=web.Application();frontend_app.router.add_get('/{path:.*}',frontend)
        fr=web.AppRunner(frontend_app);await fr.setup();fs=web.TCPSite(fr,'127.0.0.1',0);await fs.start()
        front_port=fs._server.sockets[0].getsockname()[1]
        front=f'http://127.0.0.1:{front_port}'
        badsite=web.TCPSite(fr,'127.0.0.1',0);await badsite.start()
        badfront=f'http://127.0.0.1:{badsite._server.sockets[0].getsockname()[1]}'
        s=Settings(data_dir=Path(tmp),web_password='browser-fixture-password',web_only=False,web_origins=(front,),web_url=front)
        store=Store(s);assets=Assets(s);player=MockPlayer()
        channel=SimpleNamespace(id=200,name='테스트 통화방',members=[],permissions_for=lambda m:SimpleNamespace(view_channel=True,connect=True,speak=True))
        guild=SimpleNamespace(id=100,name='로컬 검증 서버 (실제 Discord 아님)',me=object(),voice_channels=[channel],get_channel=lambda i:channel if i==200 else None)
        bot=SimpleNamespace(settings=s,store=store,assets=assets,player=player,guilds=[guild],is_ready=lambda:True,get_guild=lambda i:guild if i==100 else None)
        panel=WebPanel(bot)
        @web.middleware
        async def record(request,handler):
            if request.path == '/config.js':
                return web.Response(text='window.APPEARANCE_CONFIG = Object.freeze('+json.dumps({'API_BASE_URL':dynamic['api']})+');',content_type='text/javascript')
            requests.append({'method':request.method,'path':request.path,'origin':request.headers.get('Origin'),'auth':bool(request.headers.get('Authorization'))})
            return await handler(request)
        panel.app.middlewares.insert(0,record)
        ar=web.AppRunner(panel.app);await ar.setup();server=web.TCPSite(ar,'127.0.0.1',0);await server.start()
        api_port=server._server.sockets[0].getsockname()[1];dynamic['api']=f'http://127.0.0.1:{api_port}'
        try:
            async with async_playwright() as pw:
                kwargs={'headless':True,'args':['--no-sandbox','--test-third-party-cookie-phaseout']}
                executable=os.getenv('CHROMIUM_PATH')
                if executable:kwargs['executable_path']=executable
                browser=await pw.chromium.launch(**kwargs)
                context=await browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
                page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(front)
                await page.wait_for_function("document.querySelector('#connection-status').textContent.includes('웹 연결 정상')")
                passed.append('different-origin connection probe succeeds (separate local HTTP ports)')
                await page.screenshot(path=str(output/'firebase-railway-login.png'),full_page=True)
                await page.fill('#password',s.web_password);await page.click('#login-form button[type=submit]')
                await page.locator('#app').wait_for(state='visible')
                await page.wait_for_function("document.querySelector('#guild-select').value==='100'")
                assert await context.cookies()==[], 'Bearer login should not create cookies'
                passed.append('cross-origin login and state loading without cookies')
                await page.select_option('#voice-select','200');await page.click('#connect')
                await page.wait_for_function("document.querySelector('#voice-title').textContent.includes('연결됨')")
                assert player.last=='connect';passed.append('browser connect button reaches mock voice connector')
                await page.fill('#song-name','웹등록선수');await page.fill('#song-url','https://youtu.be/abcdefghijk')
                await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#song-count').textContent==='1'")
                assert (await store.song('A팀','웹등록선수')) is not None
                passed.append('YouTube URL registration persists through real HTTP API into SQLite')
                await page.click('#source-upload')
                await page.set_input_files('#song-file',{'name':'내등장곡.wav','mimeType':'audio/wav','buffer':wav_bytes()})
                await page.wait_for_function("document.querySelector('#upload-preview').readyState>=1 && !document.querySelector('#save-song').disabled")
                assert await page.locator('#upload-preview').evaluate('(a)=>a.src.startsWith("blob:")')
                passed.append('cross-origin multipart upload and authenticated blob audio preview')
                await page.fill('#song-name','업로드선수');await page.fill('#song-end','0:01');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#song-count').textContent==='2'")
                row=page.locator('.song-row').filter(has=page.locator('h4',has_text='업로드선수'))
                await row.get_by_role('button',name='내 기기에서 듣기',exact=True).click()
                await row.locator('audio').wait_for(state='visible')
                assert await row.locator('audio').evaluate('(a)=>a.src.startsWith("blob:")')
                passed.append('private uploaded song preview remains authenticated on the library screen')
                await row.get_by_role('button',name='▶ 재생',exact=True).click()
                assert player.last==('play','업로드선수')
                passed.append('browser playback command reaches mock Discord player')
                await page.click('.nav[data-tab=lineup]')
                await page.select_option('#lineup-grid select[data-order="1"]','웹등록선수');await page.click('#save-lineup')
                await page.wait_for_timeout(150)
                assert (await store.lineup('A팀'))['1']=='웹등록선수'
                passed.append('lineup saved through cross-origin PUT request')
                await page.click('.nav[data-tab=diagnostics]')
                async with page.expect_download() as download:
                    await page.click('#export-backup')
                path=await (await download.value).path()
                data=json.loads(Path(path).read_text())
                assert 'A팀' in data['teams'];passed.append('authenticated JSON backup download')
                await page.reload();await page.locator('#app').wait_for(state='visible')
                await page.wait_for_function("document.querySelector('#song-count').textContent==='2'")
                passed.append('sessionStorage token restores login on page reload')
                await page.screenshot(path=str(output/'firebase-railway-desktop.png'),full_page=True)
                await page.set_viewport_size({'width':390,'height':844})
                overflow=await page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert not overflow,'mobile horizontal overflow'
                await page.screenshot(path=str(output/'firebase-railway-mobile.png'),full_page=True)
                passed.append('390px mobile layout has no horizontal page overflow')
                await page.click('#logout-mobile');await page.locator('#login-screen').wait_for(state='visible')
                await page.reload();await page.wait_for_function("document.querySelector('#connection-status').textContent.includes('웹 연결 정상')")
                assert await page.locator('#app').is_hidden()
                passed.append('logout revokes token and stays logged out on reload')
                # Same-origin fallback must also work because GET commonly has no Origin header.
                same=await context.new_page();same.on('pageerror',lambda e:errors.append(str(e)))
                await same.goto(dynamic['api']);await same.fill('#password',s.web_password);await same.click('#login-form button[type=submit]')
                await same.locator('#app').wait_for(state='visible')
                await same.wait_for_function("document.querySelector('#song-count').textContent==='2'")
                passed.append('same-origin Railway/local panel still works without Origin on GET')
                bad=await context.new_page();await bad.goto(badfront)
                await bad.wait_for_function("document.querySelector('#connection-status').textContent==='연결 확인 필요'")
                passed.append('browser CORS blocks an unlisted website origin')
                assert any(r['method']=='OPTIONS' for r in requests)
                assert any(r['path']=='/api/upload' and r['auth'] and r['origin']==front for r in requests)
                assert any(r['path']=='/api/export' and r['auth'] and r['origin']==front for r in requests)
                assert not errors,errors
                passed.append('browser generated real preflights; upload/export included Authorization; no page errors')
                await browser.close()
        finally:
            await ar.cleanup();await fr.cleanup();await store.close()
        result={'passed':passed,'count':len(passed),'pageErrors':errors,
                'environment':'local two-origin Chromium + actual aiohttp/SQLite/ffprobe; Discord mocked',
                'notTested':['actual Firebase Hosting deploy','actual Railway deployment','real Discord voice','actual Firestore/YouTube']}
        (output/'browser-split-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
