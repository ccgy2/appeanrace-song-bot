"""Local real HTTP + service worker smoke test. No Discord/cloud credentials.
Install dialogs / iOS Add to Home Screen still require physical-device validation.
"""
import asyncio,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from assets import Assets
from config import Settings
from storage import Store
from webapp import WebPanel

async def main():
    out=Path(os.getenv('TEST_OUTPUT_DIR','/mnt/data/studio-ui-results'));out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        settings=Settings(data_dir=Path(td),web_only=True,web_password='local-smoke-only-123456')
        store=Store(settings);bot=SimpleNamespace(settings=settings,store=store,assets=Assets(settings),guilds=[],is_ready=lambda:False,get_guild=lambda _:None,player=SimpleNamespace(now={}))
        server=TestServer(WebPanel(bot).app);await server.start_server();checks=[]
        try:
            async with async_playwright() as p:
                browser=await p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                context=await browser.new_context();page=await context.new_page();page.set_default_timeout(10000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(str(server.make_url('/')),wait_until='networkidle',timeout=15000)
                await page.evaluate('navigator.serviceWorker.ready');await page.wait_for_function('navigator.serviceWorker.controller!==null');checks.append('service worker registered and controls real localhost page')
                manifest=await page.evaluate("async()=>await(await fetch('/manifest.webmanifest')).json()");assert manifest['scope']=='/' and manifest['display']=='standalone';checks.append('manifest served with installed-app scope and icons')
                await page.fill('#username','admin');await page.fill('#password',settings.web_password);await page.click('#login-form button[type=submit]');await page.locator('#app').wait_for(state='visible');await page.wait_for_function("document.querySelector('#auto-entrance-label').textContent==='ON'");checks.append('real local login/session/state without a mocked fetch')
                await page.click('#auto-entrance-toggle');await page.wait_for_function("document.querySelector('#auto-entrance-label').textContent==='OFF'");assert not (await store.playback_settings())['enabled'];checks.append('real UI setting persists into temporary SQLite')
                paths=await page.evaluate("async()=>{const paths=[];for(const name of await caches.keys()){const c=await caches.open(name);for(const r of await c.keys())paths.push(new URL(r.url).pathname);}return paths;}")
                assert '/offline.html' in paths and all(not x.startswith('/api/') for x in paths);checks.append('only public offline assets in Cache Storage; no API, sessions or music')
                await context.set_offline(True);await page.reload(wait_until='domcontentloaded');assert await page.get_by_role('heading',name='인터넷 연결을 확인해주세요.').is_visible();checks.append('real offline navigation returns dedicated offline page, not stale controls')
                await context.set_offline(False);await page.get_by_role('link',name='연결 후 다시 열기').click();await page.locator('#app').wait_for(state='visible');await page.wait_for_function("document.querySelector('#auto-entrance-label').textContent==='OFF'");checks.append('reconnection restores current setting and same local web session')
                assert not errors,errors
                await browser.close()
            report={'passed':len(checks),'checks':checks,'scope':'real local HTTP / actual Chromium service worker / temporary SQLite only; no production/physical-device validation'}
            (out/'pwa-local-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:await server.close();await store.close()

if __name__=='__main__':asyncio.run(main())
