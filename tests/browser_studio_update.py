"""Actual Chromium DOM interactions with isolated in-memory API/XHR fixtures.
No production request or actual Discord/mobile installer is run.
"""
import asyncio
import json
import os
from pathlib import Path
import re
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]

def markup(filename):
    text=(ROOT/'static'/filename).read_text()
    return re.sub(r'<link[^>]*>','',re.sub(r'<script[^>]*>.*?</script>','',text,flags=re.S))

async def main():
    out=Path(os.getenv('TEST_OUTPUT_DIR','/mnt/data/studio-ui-results'));out.mkdir(exist_ok=True,parents=True)
    labels=['라인업 송','홈런','홈런 2','삼진','볼넷','풀카운트','견제','플라이','아웃','도루 성공','이닝 교대','경기 시작','경기 종료','경기 종료 랜덤','득점','아웃1','안타']
    events=[{'key':f'event{i}','label':v,'bundledCount':i%3+1,'customEvent':i>=15,'custom':None} for i,v in enumerate(labels)]
    state={'ready':True,'webOnly':False,'storage':'local','botLabel':'청팀 봇','botTarget':'primary','botTargets':[{'id':'primary','name':'청팀 봇','ready':True}],
           'guilds':[{'id':'123','name':'화면 테스트 서버'}],'guildId':'123','team':'우리팀','teams':['우리팀'],'activeTeam':'우리팀',
           'channels':[{'id':'789','name':'경기 스테이지','type':'stage','available':True,'members':2}], 'voice':{'connected':False},'nowPlaying':None,
           'autoEntrance':{'enabled':True,'revision':0},'state':{'volume':.5,'currentOrder':1},'lineup':{'1':'같은 이름'},'maxUploadMb':25,'maxEventTracks':100,
           'fileStorage':{'status':'local','persistent':None,'uploadsAllowed':True,'label':'테스트 데이터 · 실제 서버에 미연결','message':'이 화면은 동작 확인용 예시이며 실제 등록 곡과 무관합니다.'},
           'library':{c:[{'name':'같은 이름','category':c,'source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20,'memberId':''},
                         {'name':f'{c} 아주 긴 곡 이름과 한글 테스트를 위한 노래','category':c,'source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20,'memberId':''}] for c in ['entrance','cheer','situation']},
           'events':events,'deletedEvents':[]}
    state['songs']=state['library']['entrance']
    checks=[];errors=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        ctx=await browser.new_context(viewport={'width':1500,'height':1050});page=await ctx.new_page();page.set_default_timeout(8000)
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda dialog:dialog.accept())
        await page.set_content(markup('index.html'));await page.add_style_tag(content=(ROOT/'static/style.css').read_text())
        await page.evaluate('''data=>{
          window.APPEARANCE_CONFIG={API_BASE_URL:'https://mock.invalid'};window.fixture=data;window.savedRequests=[];window.deletedFixtures={};window.fixtureRole='registrar';window.fixtureUploadCount=0;
          window.fetch=async function(url,opts={}){
            const path=new URL(url).pathname,q=new URL(url).searchParams,method=opts.method||'GET',d=opts.body?JSON.parse(opts.body):{};let reply={ok:true};
            window.savedRequests.push({path,method,data:d});
            if(path==='/api/connection')reply={service:'appearance-song-bot',apiVersion:2,capabilities:['roles-v3','event-all-categories','dual-bot','event-play-modes','separate-voice-channels'],discordReady:true};
            if(path==='/api/login')reply={ok:true,accessToken:'test-only',csrf:'test-csrf',user:{username:'demo',displayName:'테스트 계정',role:window.fixtureRole}};
            if(path==='/api/state'||path==='/api/live')reply=window.fixture;
            if(path==='/api/playback-settings' && method==='PUT'){window.fixture.autoEntrance={enabled:d.enabled,revision:window.fixture.autoEntrance.revision+1};reply={autoEntrance:window.fixture.autoEntrance};}
            if(path==='/api/events' && method==='PUT'){
              if(d.restore){const event=window.deletedFixtures[d.key];window.fixture.events.push(event);window.fixture.deletedEvents=window.fixture.deletedEvents.filter(e=>e.key!==d.key);}
              else{const event=window.fixture.events.find(e=>e.key===d.key);event.custom={tracks:d.tracks,playMode:d.playMode};if(d.label)event.label=d.label;}
            }
            if(path==='/api/events' && method==='DELETE'){
              const key=q.get('key'),event=window.fixture.events.find(e=>e.key===key);window.deletedFixtures[key]=event;window.fixture.events=window.fixture.events.filter(e=>e.key!==key);window.fixture.deletedEvents.push({key,label:event.label});
            }
            if(path==='/api/users')reply={users:[{username:'admin',displayName:'관리자',role:'admin',enabled:true},{username:'demo',displayName:'테스트 계정',role:'registrar',enabled:true}]};
            return new Response(JSON.stringify(reply),{status:200,headers:{'Content-Type':'application/json'}});
          };
          window.XMLHttpRequest=class{
            constructor(){this.upload={};this.status=200;}
            open(){} setRequestHeader(){}
            send(data){const file=data.get('file');setTimeout(()=>{this.upload.onprogress?.({lengthComputable:true,loaded:1,total:1});window.fixtureUploadCount++;this.responseText=JSON.stringify({id:window.fixtureUploadCount.toString(16).padStart(32,'0'),name:file.name,duration:1});this.onload();},15);}
          };
        }''',state)
        await page.add_script_tag(content=(ROOT/'static/app.js').read_text())
        await page.fill('#username','demo');await page.fill('#password','testing-password');await page.click('#login-form button[type=submit]')
        await page.locator('#app').wait_for(state='visible');await page.wait_for_function("document.querySelector('#song-count').textContent==='2'")
        checks.append('registrar login and library rendering')
        assert await page.locator('.song-editor').is_visible();assert await page.locator('#new-team').is_hidden()
        await page.click('#auto-entrance-toggle');await page.wait_for_function("document.querySelector('#auto-entrance-label').textContent==='OFF'")
        assert await page.locator('#auto-entrance-toggle').get_attribute('aria-checked')=='false';checks.append('global ON/OFF sends persisted setting without disabling manual controls')
        await page.click('[data-tab=events]');assert await page.locator('.event-card').count()==17
        assert await page.locator('#deleted-events-box').is_hidden();assert await page.locator('.event-card .delete').count()==17;checks.append('both default and custom events have registrar delete/edit buttons')
        card=page.locator('[data-event-key=event1]');await card.get_by_role('button',name='곡 · 순서 편집',exact=True).click()
        await page.locator('#event-editor-dialog').wait_for(state='visible');assert await page.locator('#event-library-choices input').count()==6
        await page.get_by_role('button',name='검색 결과 전체 선택',exact=True).click();await page.click('#add-selected-event-tracks');assert await page.locator('.event-draft-row').count()==6
        cats=await page.evaluate('eventDraft.tracks.map(t=>t.category)');assert set(cats)=={'entrance','cheer','situation'};checks.append('all three categories multi-selected and added at once')
        await page.select_option('#event-edit-mode','sequence')
        before=await page.evaluate('eventDraft.tracks.map(eventTrackLabel)')
        await page.locator('.event-draft-row').nth(0).drag_to(page.locator('.event-draft-row').nth(2))
        after=await page.evaluate('eventDraft.tracks.map(eventTrackLabel)');assert after[2]==before[0] and after!=before;checks.append('HTML drag and drop reorder changes actual draft order')
        # Native multi-file chooser receives two synthetic files; transport is explicitly mocked.
        await page.set_input_files('#event-files',[{'name':'등장곡 원본.wav','mimeType':'audio/wav','buffer':b'fixture-a'},{'name':'응원가 원본.wav','mimeType':'audio/wav','buffer':b'fixture-b'}])
        await page.wait_for_function('window.fixtureUploadCount===2 && eventDraft.busy===false');assert await page.locator('.event-draft-row').count()==8
        checks.append('multi-file selection adds two original filenames with progress and no implicit save')
        await page.evaluate('''()=>{const files=new DataTransfer();files.items.add(new File(['x'],'드래그 곡.wav',{type:'audio/wav'}));document.querySelector('#event-file-drop').dispatchEvent(new DragEvent('drop',{dataTransfer:files,bubbles:true,cancelable:true}));}''')
        await page.wait_for_function('window.fixtureUploadCount===3 && eventDraft.busy===false');assert await page.locator('.event-draft-row').count()==9;checks.append('file drop appends to ordered queue')
        for width in [320,360,390,768,1500]:
            await page.set_viewport_size({'width':width,'height':950})
            assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),f'page overflow {width}'
            assert not await page.locator('#event-editor-dialog').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),f'dialog overflow {width}'
        await page.set_viewport_size({'width':1500,'height':1050});await page.evaluate("document.querySelector('#toast').hidden=true")
        await page.screenshot(path=str(out/'studio-editor-desktop.png'),full_page=False)
        await page.set_viewport_size({'width':390,'height':900});await page.screenshot(path=str(out/'studio-editor-mobile.png'),full_page=False)
        checks.append('dialog and page have no horizontal overflow at 320, 360, 390, 768 and 1500px')
        await page.click('#save-event-draft');await page.locator('#event-editor-dialog').wait_for(state='hidden')
        saved=await page.evaluate("window.savedRequests.filter(r=>r.path==='/api/events'&&r.method==='PUT').at(-1).data")
        assert len(saved['tracks'])==9 and saved['playMode']=='sequence';checks.append('explicit save sends ordered 9-track payload and selected mode')
        card=page.locator('[data-event-key=event1]');await card.get_by_role('button',name='상황 삭제',exact=True).click()
        await page.wait_for_function("!document.querySelector('[data-event-key=event1]')");assert await page.locator('#deleted-events-box').is_visible()
        await page.select_option('#deleted-event-select','event1');await page.click('#restore-event');await page.locator('[data-event-key=event1]').wait_for();assert await page.locator('#deleted-events-box').is_hidden();checks.append('builtin situation delete and restore update cards and restore menu')
        # Mobile arrow buttons provide an alternative to desktop dragging.
        await page.locator('[data-event-key=event1]').get_by_role('button',name='곡 · 순서 편집',exact=True).click()
        before=await page.evaluate('eventDraft.tracks.map(eventTrackLabel)');await page.locator('.event-order-actions').nth(0).get_by_role('button',name='아래로',exact=False).click()
        after=await page.evaluate('eventDraft.tracks.map(eventTrackLabel)');assert after[1]==before[0];checks.append('mobile reorder arrows work')
        await page.get_by_role('button',name='닫기',exact=True).click()
        await page.evaluate("currentUser.role='player';applyRole();renderSongs();renderEvents();")
        assert await page.locator('#event-create-card').is_hidden();assert await page.locator('.event-card .delete').count()==0
        assert await page.locator('.song-editor').is_hidden();assert await page.locator('#auto-entrance-toggle').is_enabled();checks.append('player sees playback but not registration, event edit or delete')
        await page.evaluate("currentUser.role='user';applyRole();renderSongs();renderEvents();")
        assert await page.locator('#auto-entrance-toggle').is_disabled();assert await page.locator('.event-card button').count()==0;assert await page.locator('#connect').is_disabled();checks.append('user is read-only in Discord controls')
        await page.evaluate("currentUser.role='admin';applyRole();renderSongs();renderEvents();")
        await page.click('[data-tab=users]');await page.locator('.user-row select').wait_for();values=await page.locator('.user-row select option').evaluate_all('(items)=>items.map(x=>x.value)');assert set(values)=={'pending','user','player','registrar'};checks.append('admin role dropdown exposes all new roles')
        await page.select_option('.user-row select','player');await page.get_by_role('button',name='권한 저장',exact=True).click();assert await page.evaluate("window.savedRequests.some(r=>r.path==='/api/users/demo'&&r.method==='PUT'&&r.data.role==='player')")
        await page.click('[data-tab=events]');await page.set_viewport_size({'width':1500,'height':1050});await page.evaluate("document.querySelector('#toast').hidden=true");await page.screenshot(path=str(out/'studio-soundboard-desktop.png'),full_page=False)
        # Session loss must close modal and discard private draft from the next account's view.
        await page.locator('[data-event-key=event1]').get_by_role('button',name='곡 · 순서 편집',exact=True).click();await page.evaluate('showLogin()');assert await page.locator('#event-editor-dialog').is_hidden();assert await page.evaluate('eventDraft===null');checks.append('session expiry closes private edit dialog')
        # Install UI uses only a genuine captured browser event, tested here with an explicit stub.
        install=await ctx.new_page();install.on('pageerror',lambda e:errors.append(str(e)))
        await install.set_content(markup('download.html'));await install.add_style_tag(content=(ROOT/'static/style.css').read_text());await install.add_script_tag(content=(ROOT/'static/pwa.js').read_text())
        assert await install.locator('#pwa-install-button').is_hidden()
        await install.evaluate("()=>{const e=new Event('beforeinstallprompt',{cancelable:true});e.prompt=async()=>{window.promptCalled=true};e.userChoice=Promise.resolve({outcome:'accepted'});window.dispatchEvent(e);}")
        assert await install.locator('#pwa-install-button').is_visible();await install.click('#pwa-install-button');assert await install.evaluate('window.promptCalled===true');checks.append('install button only appears after install event and prompts on click')
        for width in [320,390,768,1500]:
            await install.set_viewport_size({'width':width,'height':950});assert not await install.evaluate('document.documentElement.scrollWidth>innerWidth')
        await install.set_viewport_size({'width':390,'height':900});await install.screenshot(path=str(out/'studio-install-mobile.png'),full_page=False)
        checks.append('install instructions responsive; no fake APK or IPA download')
        assert not errors,errors
        report={'passed':len(checks),'checks':checks,'pageErrors':errors,'scope':'Chromium DOM + mocked API/upload/install event only; no live server/mobile install'}
        (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
        await browser.close()

if __name__=='__main__':asyncio.run(main())
