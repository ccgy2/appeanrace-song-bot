"""In-memory DOM/UI checks. Does not navigate to network hosts or test real browser CORS.
Full local-network browser integration was blocked by this environment's browser policy.
"""
import asyncio
import json
import os
from pathlib import Path
import re
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]

async def main():
    out=Path(os.getenv('TEST_OUTPUT_DIR','/mnt/data/library-ui-results'));out.mkdir(exist_ok=True,parents=True)
    state={
        'ready':False,'webOnly':True,'storage':'firebase','guilds':[],'guildId':None,'teams':['우리팀'],
        'team':'우리팀','activeTeam':'우리팀','channels':[],'voice':None,'nowPlaying':None,
        'state':{'volume':.5,'currentOrder':1},'lineup':{'1':'김선수'},'maxUploadMb':25,
        'fileStorage':{'status':'local','persistent':None,'uploadsAllowed':True,'label':'화면 예시 · 로컬 테스트 데이터',
            'message':'실제 Firebase/Railway 서버에 연결한 화면이 아닙니다. 영구 볼륨 연결 상태는 배포 후 확인하세요.'},
        'library':{
            'entrance':[{'name':'김선수','category':'entrance','source':'youtube','url':'https://youtu.be/abcdefghijk','start':10,'end':40,'memberId':'123456789012345678'},
                        {'name':'타순에 등록된 다른 선수','category':'entrance','source':'upload','assetId':'a'*32,'start':0,'end':30,'fileMissing':True}],
            'cheer':[{'name':'김선수','category':'cheer','source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':120,'memberId':''}],
            'situation':[{'name':'홈런 축하곡','category':'situation','source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20,'memberId':''}]
        },
        'events':[{'key':'homerun','label':'홈런','custom':None,'bundledCount':1}]
    }
    state['songs']=state['library']['entrance']
    markup=(ROOT/'static/index.html').read_text()
    markup=re.sub(r'<script[^>]*>.*?</script>','',markup,flags=re.S)
    markup=re.sub(r'<link[^>]*>','',markup)
    checks=[];errors=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        context=await browser.new_context(viewport={'width':1440,'height':1000})
        page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        # Everything stays on about:blank; no policy-blocked navigation is retried.
        await page.set_content(markup)
        await page.add_style_tag(content=(ROOT/'static/style.css').read_text())
        await page.evaluate('''(data)=>{
            window.APPEARANCE_CONFIG={API_BASE_URL:'https://mock.invalid'};
            window.fixture=data;window.savedRequests=[];
            window.fetch=async function(url,opts={}){
                const path=new URL(url).pathname;let reply={};
                if(path==='/api/connection')reply={service:'appearance-song-bot',apiVersion:2,capabilities:['library-categories'],discordReady:false,webOnly:true};
                if(path==='/api/login')reply={ok:true,accessToken:'offline-test-only',csrf:'offline-csrf',user:{username:'admin',displayName:'관리자',role:'admin'}};
                if(path==='/api/state')reply=window.fixture;
                if(path==='/api/songs'&&opts.method==='POST'){
                    const d=JSON.parse(opts.body);window.savedRequests.push(d);
                    const rows=window.fixture.library[d.category];const i=rows.findIndex(s=>s.name===(d.oldName||d.name));
                    if(i>=0)rows[i]={...rows[i],...d};else rows.push(d);
                    if(d.category==='entrance')window.fixture.songs=rows;
                    reply={ok:true};
                }
                return new Response(JSON.stringify(reply),{status:200,headers:{'Content-Type':'application/json'}});
            };
        }''',state)
        await page.add_script_tag(content=(ROOT/'static/app.js').read_text())
        assert await page.locator('#app').is_hidden();checks.append('login gate visible before mock login')
        await page.fill('#username','admin');await page.fill('#password','offline-test-password');await page.click('#login-form button[type=submit]')
        await page.locator('#app').wait_for(state='visible')
        await page.wait_for_function("document.querySelector('#song-count').textContent==='2'")
        checks.append('fixture entrance list and missing-file record rendered without hiding records')
        row=page.locator('.song-row').filter(has=page.get_by_role('heading',name='김선수',exact=True))
        await row.get_by_role('button',name='수정',exact=True).click()
        assert not await page.locator('#song-name').evaluate('(e)=>e.readOnly')
        await page.fill('#song-name','새 닉네임');await page.click('#save-song')
        await page.wait_for_function("window.savedRequests.length===1")
        d=await page.evaluate('window.savedRequests[0]')
        assert d['oldName']=='김선수' and d['name']=='새 닉네임' and d['memberId']=='123456789012345678' and d['category']=='entrance'
        checks.append('editable nickname sends oldName and preserves member ID in save payload')
        await page.click('[data-category=cheer]')
        assert await page.locator('#song-count').text_content()=='1'
        assert await page.locator('.song-name').text_content()=='김선수'
        assert await page.locator('#auto-song-settings').is_hidden()
        checks.append('cheer tab independent of entrance and has no auto-entry editor')
        await page.click('[data-category=situation]')
        assert await page.locator('.song-name').text_content()=='홈런 축하곡'
        assert await page.locator('#editor-title').text_content()=='새 상황별 노래 등록'
        await page.click('.nav[data-tab=events]')
        assert await page.locator('.event-card select option[value="홈런 축하곡"]').count()==1
        checks.append('situation tab and admin soundboard selector render separately')
        await page.click('.nav[data-tab=songs]');await page.click('[data-category=entrance]')
        await page.evaluate("document.querySelector('#toast').hidden=true")
        await page.screenshot(path=str(out/'library-desktop.png'),full_page=True)
        for width in [360,390,768,1440]:
            await page.set_viewport_size({'width':width,'height':900})
            assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
        await page.set_viewport_size({'width':390,'height':900})
        await page.screenshot(path=str(out/'library-mobile.png'),full_page=True)
        checks.append('no horizontal overflow at 360, 390, 768, 1440px')
        await page.evaluate("currentUser={username:'player1',displayName:'재생자',role:'player'};applyRole();renderSongs();renderEvents();")
        assert await page.locator('.song-editor').is_visible()
        assert await page.locator('#new-team').is_hidden()
        assert await page.locator('#export-audio-backup').is_hidden()
        assert await page.locator('.song-row button.delete').count()==0
        await page.click('.nav[data-tab=events]')
        assert await page.locator('.event-card select').count()==0
        checks.append('player can edit/upload; admin delete, binding, backup controls are absent/hidden')
        await page.evaluate("window.fixture.fileStorage={status:'unsafe',uploadsAllowed:false,label:'영구 저장 설정 필요',message:'Railway Volume을 연결하세요.'};drawStorage(window.fixture);")
        assert await page.locator('#song-file').is_disabled()
        assert 'unsafe' in (await page.locator('#storage-notice').get_attribute('class'))
        checks.append('unsafe storage warning disables new file selection')
        await page.click('#logout-mobile');await page.locator('#login-screen').wait_for(state='visible')
        assert await page.locator('#app').is_hidden();checks.append('logout returns to login screen')
        assert not errors,errors
        await browser.close()
    result={'count':len(checks),'passed':checks,'pageErrors':errors,'scope':'offline about:blank DOM + synthetic fetch fixtures; not browser HTTP/CORS or live cloud'}
    (out/'offline-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':asyncio.run(main())
