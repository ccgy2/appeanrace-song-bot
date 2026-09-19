"""Real local browser + two HTTP origins + SQLite/ffprobe. Discord is deliberately mocked."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import wave
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from config import Settings, ROOT
from assets import Assets
from storage import Store
from webapp import WebPanel

class FakePlayer:
    def __init__(self):
        self.now={};self.last=None
        self.voice=SimpleNamespace(status=lambda g:{'connected':False,'channelId':None,'channelName':None,'latencyMs':None,'serverMuted':False,'error':None})
    async def play(self,guild,team,song,**kwargs):self.last=(song['category'],song['name']);return True
    async def event(self,*args):self.last=('event',args[2]);return True

def wav():
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*24000)
    return b.getvalue()

async def main():
    output=Path(os.getenv('TEST_OUTPUT_DIR', '/mnt/data/library-ui-results'));output.mkdir(parents=True,exist_ok=True)
    results=[]; errors=[]; api_url=''
    with tempfile.TemporaryDirectory() as tmp:
        async def front(request):
            name=request.path.lstrip('/') or 'index.html'
            if name=='config.js':
                return web.Response(text='window.APPEARANCE_CONFIG='+json.dumps({'API_BASE_URL':api_url})+';',content_type='application/javascript')
            if name not in {'app.js','style.css','index.html'}:raise web.HTTPNotFound()
            resp=web.FileResponse(ROOT/'static'/name)
            resp.headers['Content-Security-Policy']=("default-src 'self'; script-src 'self'; style-src 'self'; "
                f"connect-src 'self' {api_url}; media-src 'self' blob:; img-src 'self' data:; object-src 'none'")
            return resp
        fapp=web.Application();fapp.router.add_get('/{path:.*}',front)
        fs=TestServer(fapp);await fs.start_server();front_url=str(fs.make_url('/')).rstrip('/')
        settings=Settings(data_dir=Path(tmp),web_password='browser-test-password-123',web_origins=(front_url,),web_url=front_url)
        store=Store(settings);assets=Assets(settings);player=FakePlayer()
        ch=SimpleNamespace(id=200,name='테스트 통화방',members=[],permissions_for=lambda _:SimpleNamespace(view_channel=True,connect=True,speak=True))
        g=SimpleNamespace(id=100,name='테스트 서버 · 실제 Discord 아님',voice_channels=[ch],get_channel=lambda _:ch,me=object())
        bot=SimpleNamespace(settings=settings,store=store,assets=assets,player=player,guilds=[g],get_guild=lambda _:g,is_ready=lambda:True)
        panel=WebPanel(bot);api=TestServer(panel.app);await api.start_server();api_url=str(api.make_url('/')).rstrip('/')
        try:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                context=await browser.new_context(viewport={'width':1440,'height':1050},accept_downloads=True)
                page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.set_default_timeout(15000)
                await page.goto(front_url)
                await page.wait_for_function("document.querySelector('#connection-status').textContent.includes('웹 연결 정상')")
                assert await page.locator('#app').is_hidden()
                await page.fill('#username','admin');await page.fill('#password',settings.web_password);await page.click('#login-form button[type=submit]')
                await page.locator('#app').wait_for(state='visible');await page.wait_for_function("document.querySelector('#guild-select').value==='100'")
                results.append('separate-origin login with Bearer/CSRF and real local API; no private state before login')
                assert not await context.cookies()
                await page.fill('#song-name','김선수');await page.fill('#song-url','https://youtu.be/abcdefghijk')
                await page.locator('#auto-song-settings summary').click();await page.fill('#member-id','123456789012345678');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#count-entrance').textContent==='1'")
                await page.click('.nav[data-tab=lineup]');await page.select_option('#lineup-grid select[data-order="1"]','김선수');await page.click('#save-lineup')
                await page.wait_for_function("document.querySelector('#toast').textContent.includes('타순을 저장')")
                await page.click('.nav[data-tab=songs]');await page.locator('.song-row').get_by_role('button',name='수정',exact=True).click()
                assert not await page.locator('#song-name').evaluate('(x)=>x.readOnly')
                await page.fill('#song-name','새닉네임');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('.song-name').textContent==='새닉네임'")
                assert (await store.lineup('A팀'))['1']=='새닉네임'
                assert (await store.song('A팀','새닉네임'))['memberId']=='123456789012345678'
                assert len(await store.songs('A팀'))==1
                results.append('library edit changes nickname atomically with lineup and user ID preserved')
                await page.click('[data-category=cheer]');await page.click('#source-upload')
                await page.set_input_files('#song-file',{'name':'응원가.wav','mimeType':'audio/wav','buffer':wav()})
                await page.wait_for_function("document.querySelector('#upload-preview').readyState>=1 && !document.querySelector('#save-song').disabled")
                assert await page.input_value('#song-end')=='0:03'
                await page.fill('#song-name','새닉네임');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#count-cheer').textContent==='1'")
                assert (await store.song('A팀','새닉네임','cheer'))['source']=='upload'
                results.append('cheer upload uses actual ffprobe, whole-file default and same nickname independent of entrance')
                await page.locator('.song-row').get_by_role('button',name='▶ 재생',exact=True).click()
                await page.wait_for_timeout(100);assert player.last==('cheer','새닉네임')
                results.append('category-specific play reaches mocked Discord player')
                async with page.expect_download() as download:
                    await page.locator('.song-row').get_by_role('button',name='원본 다운로드',exact=True).click()
                path=await (await download.value).path();assert Path(path).read_bytes()==wav()
                results.append('authenticated original audio download matches uploaded bytes')
                await page.click('[data-category=situation]');await page.fill('#song-name','홈런 축하곡');await page.fill('#song-url','https://youtu.be/abcdefghijk');await page.click('#save-song')
                await page.wait_for_function("document.querySelector('#count-situation').textContent==='1'")
                await page.click('.nav[data-tab=events]')
                card=page.locator('.event-card').filter(has=page.get_by_role('heading',name='홈런',exact=True))
                await card.locator('select').select_option('홈런 축하곡');await card.get_by_role('button',name='선택한 노래 연결').click()
                await page.wait_for_function("document.querySelector('#toast').textContent.includes('경기 사운드에 연결')")
                assert (await store.event('A팀','homerun'))['songName']=='홈런 축하곡'
                results.append('situation library can be bound to home-run soundboard')
                await page.click('.nav[data-tab=songs]');await page.click('[data-category=cheer]')
                await page.reload();await page.locator('#app').wait_for(state='visible')
                await page.wait_for_function("document.querySelector('[data-category=cheer]').getAttribute('aria-pressed')==='true'")
                await page.wait_for_function("document.querySelector('.song-name').textContent==='새닉네임'")
                results.append('page reload keeps chosen category/team and reloads server-stored songs')
                await page.screenshot(path=str(output/'library-desktop.png'),full_page=True)
                for width in [360,390,768]:
                    await page.set_viewport_size({'width':width,'height':900})
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
                await page.set_viewport_size({'width':390,'height':900});await page.screenshot(path=str(output/'library-mobile.png'),full_page=True)
                results.append('360/390/768px library has no horizontal overflow')
                await page.set_viewport_size({'width':1440,'height':1050});await page.click('.nav[data-tab=diagnostics]')
                async with page.expect_download() as download:await page.click('#export-audio-backup')
                path=await (await download.value).path()
                with zipfile.ZipFile(path) as z:
                    assert 'metadata.json' in z.namelist();assert any(n.endswith('.wav') for n in z.namelist())
                results.append('admin ZIP backup downloads metadata and actual audio')
                await page.click('#logout');await page.locator('#login-screen').wait_for(state='visible');await page.reload()
                await page.wait_for_function("document.querySelector('#connection-status').textContent.includes('웹 연결 정상')")
                assert await page.locator('#app').is_hidden()
                results.append('logout remains logged out after reload')
                assert not errors,errors
                results.append('no uncaught browser JavaScript errors')
                await browser.close()
        finally:
            await api.close();await fs.close();await store.close()
    result={'checks':results,'count':len(results),'pageErrors':errors,'scope':'real local Chromium/HTTP/SQLite/ffprobe; simulated Discord; no Firebase/Railway cloud deployment'}
    (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':asyncio.run(main())
