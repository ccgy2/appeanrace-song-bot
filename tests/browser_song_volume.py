"""Chromium UI tests for song gain. In-memory API only; no production/OS install."""
import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright
from browser_studio_update import markup
ROOT=Path(__file__).resolve().parents[1]

async def main():
    out=Path(os.getenv('TEST_OUTPUT_DIR','/mnt/data/song-volume-ui'));out.mkdir(parents=True,exist_ok=True)
    lib={
      'entrance':[{'name':'김선수 등장곡','category':'entrance','source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20,'memberId':'123456789012345678','volumePercent':80}],
      'cheer':[{'name':'우리 팀 응원가','category':'cheer','source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20,'volumePercent':150}],
      'situation':[{'name':'기존 볼륨 정보 없는 곡','category':'situation','source':'youtube','url':'https://youtu.be/abcdefghijk','start':0,'end':20}],
    }
    fixture={'ready':True,'webOnly':False,'storage':'local','botLabel':'청팀 봇','botTarget':'primary','botTargets':[{'id':'primary','name':'청팀 봇','ready':True}],
      'guilds':[{'id':'123','name':'테스트 서버'}],'guildId':'123','team':'테스트팀','teams':['테스트팀'],'activeTeam':'테스트팀',
      'channels':[{'id':'789','name':'테스트 음성방','type':'voice','available':True,'members':2}], 'voice':{'connected':False},'nowPlaying':None,
      'autoEntrance':{'enabled':True,'revision':0},'state':{'volume':.5,'currentOrder':1},'lineup':{},'maxUploadMb':25,'maxEventTracks':100,'songVolumeEnabled':True,
      'fileStorage':{'status':'local','persistent':None,'uploadsAllowed':True,'label':'화면 테스트용 예시 데이터','message':'실제 운영 계정·음원과 무관한 테스트 화면입니다.'},
      'library':lib,'songs':lib['entrance'],'deletedEvents':[],
      'events':[{'key':'homerun','label':'홈런','customEvent':False,'bundledCount':3,'custom':None},
        {'key':'custom_demo','label':'여러 곡 볼륨 테스트','customEvent':True,'bundledCount':0,'custom':{'playMode':'sequence','tracks':[
          {'type':'song','category':'entrance','songName':'김선수 등장곡'},
          {'type':'asset','assetId':'a'*32,'filename':'직접 업로드한 응원가.wav','volumePercent':0},
          {'type':'asset','assetId':'b'*32,'filename':'아주 긴 이름의 경기 상황 오디오 테스트 파일입니다.wav','volumePercent':160}]}},
        {'key':'out','label':'구버전 지정 파일','customEvent':False,'bundledCount':1,'custom':{'file':'out.mp3','volumePercent':80}}]}
    checks=[];errors=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        ctx=await browser.new_context(viewport={'width':1500,'height':1050});page=await ctx.new_page();page.set_default_timeout(9000)
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
        await page.set_content(markup('index.html'));await page.add_style_tag(content=(ROOT/'static/style.css').read_text())
        await page.evaluate('''data=>{
          window.APPEARANCE_CONFIG={API_BASE_URL:'https://mock.invalid'};window.fixture=data;window.savedRequests=[];
          window.fetch=async (url,opts={})=>{
            const path=new URL(url).pathname,method=opts.method||'GET',d=opts.body?JSON.parse(opts.body):{};
            window.savedRequests.push({path,method,data:d});let reply={ok:true};
            if(path==='/api/connection')reply={service:'appearance-song-bot',apiVersion:2,capabilities:['song-volume','roles-v3','event-all-categories','dual-bot','event-play-modes','separate-voice-channels'],discordReady:true};
            if(path==='/api/login')reply={accessToken:'fixture-only',csrf:'fixture-only',user:{username:'demo',displayName:'볼륨 테스트',role:'registrar'}};
            if(path==='/api/state' || path==='/api/live')reply=window.fixture;
            if(path==='/api/songs' && method==='POST'){
              const list=window.fixture.library[d.category],i=list.findIndex(x=>x.name===(d.oldName ?? d.name));
              if(i<0)list.push(d);else list[i]={...list[i],...d};
              window.fixture.songs=window.fixture.library.entrance;
            }
            if(path==='/api/events' && method==='PUT'){
              const event=window.fixture.events.find(x=>x.key===d.key);
              event.custom={...event.custom,...d};
            }
            return new Response(JSON.stringify(reply),{status:200,headers:{'Content-Type':'application/json'}});
          };
        }''',fixture)
        await page.add_script_tag(content=(ROOT/'static/app.js').read_text())
        await page.fill('#username','demo');await page.fill('#password','example-only');await page.click('#login-form button[type=submit]')
        await page.locator('#app').wait_for(state='visible');await page.wait_for_function('state?.songVolumeEnabled===true')
        assert await page.input_value('#song-volume')=='100'
        assert await page.input_value('#song-volume-number')=='100'
        assert await page.locator('.song-volume-badge').inner_text()=='곡 80%'
        checks.append('registrar sees independent gain widget and saved per-song badge')
        await page.locator('.song-row').get_by_role('button',name='수정',exact=True).click()
        assert await page.input_value('#song-volume-number')=='80';assert await page.input_value('#member-id')=='123456789012345678'
        checks.append('editing loads saved gain without changing Discord ID or source')
        await page.locator('#song-volume').press('Home');assert await page.input_value('#song-volume-number')=='0'
        await page.fill('#song-volume-number','145');assert await page.input_value('#song-volume')=='145'
        assert await page.locator('#song-volume-control .volume-warning').is_visible()
        await page.get_by_role('button',name='곡 자체 볼륨 100%로 초기화',exact=True).click();assert await page.input_value('#song-volume-number')=='100'
        checks.append('slider, numeric entry, zero, reset, and boost warning stay synchronized')
        before=await page.evaluate("savedRequests.filter(r=>r.path==='/api/songs').length")
        for value in ['201','-1','12.5','']:
            await page.fill('#song-volume-number',value);await page.click('#save-song')
            assert not await page.locator('#song-volume-number').evaluate('(e)=>e.checkValidity()')
            assert await page.evaluate("savedRequests.filter(r=>r.path==='/api/songs').length")==before
        checks.append('invalid, fractional, blank, negative and above-200 inputs block saving')
        await page.fill('#song-volume-number','145');await page.click('#save-song')
        await page.wait_for_function("fixture.library.entrance[0].volumePercent===145 && document.querySelector('#song-volume-number').value==='100'")
        assert await page.locator('.song-volume-badge').inner_text()=='곡 145%'
        assert await page.evaluate("fixture.state.volume===0.5 && !savedRequests.some(r=>r.path==='/api/control'||r.path==='/api/upload')")
        payload=await page.evaluate("savedRequests.filter(r=>r.path==='/api/songs').at(-1).data")
        assert payload['oldName']=='김선수 등장곡' and payload['memberId']=='123456789012345678' and payload['volumePercent']==145
        checks.append('song save sends gain and preserves master/source/ID; no audio rewrite or playback request')
        await page.click('[data-category=cheer]');await page.locator('.song-row').get_by_role('button',name='수정',exact=True).click()
        assert await page.input_value('#song-volume-number')=='150'
        await page.click('[data-category=situation]');await page.locator('.song-row').get_by_role('button',name='수정',exact=True).click()
        assert await page.input_value('#song-volume-number')=='100'
        checks.append('all library categories work and old missing metadata defaults to 100%')
        await page.evaluate('state.songVolumeEnabled=false');before=await page.evaluate('savedRequests.length')
        await page.click('#save-song');assert '봇 패치' in await page.locator('#toast').inner_text()
        assert not await page.evaluate(f"savedRequests.slice({before}).some(r=>r.path==='/api/songs')")
        await page.evaluate('state.songVolumeEnabled=true')
        checks.append('new UI refuses to silently save gain against an old backend')
        await page.click('[data-category=entrance]');await page.locator('.song-row').get_by_role('button',name='수정',exact=True).click()
        await page.evaluate("document.querySelector('#toast').hidden=true")
        await page.locator('#song-volume-control').scroll_into_view_if_needed()
        await page.locator('.song-editor').screenshot(path=str(out/'song-volume-editor-desktop.png'))
        for width in [320,360,390,768,1500]:
            await page.set_viewport_size({'width':width,'height':1000})
            assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
            assert not await page.locator('#song-volume-control').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),width
        await page.set_viewport_size({'width':390,'height':900});await page.locator('#song-volume-control').scroll_into_view_if_needed()
        await page.locator('.song-editor').screenshot(path=str(out/'song-volume-editor-mobile.png'))
        checks.append('song editor has no horizontal overflow at 320/360/390/768/1500px')
        await page.click('[data-tab=events]');await page.locator('[data-event-key=homerun]').get_by_role('button',name='곡 · 순서 편집',exact=True).click()
        await page.fill('#event-default-volume-number','0');await page.click('#save-event-draft')
        await page.locator('#event-editor-dialog').wait_for(state='hidden')
        assert '곡 0%' in await page.locator('[data-event-key=homerun] .event-summary').inner_text()
        await page.locator('[data-event-key=homerun]').get_by_role('button',name='곡 · 순서 편집',exact=True).click()
        assert await page.input_value('#event-default-volume-number')=='0';await page.get_by_role('button',name='닫기',exact=True).click()
        checks.append('builtin sound volume saves and reopens at 0% instead of falling back to 100%')
        await page.locator('[data-event-key=custom_demo]').get_by_role('button',name='곡 · 순서 편집',exact=True).click()
        assert await page.locator('#event-default-volume-control').count()==0
        assert '145%' in await page.locator('.event-linked-volume').inner_text()
        assert await page.input_value('#event-track-volume-1-number')=='0'
        assert await page.input_value('#event-track-volume-2-number')=='160'
        checks.append('linked library song displays its own gain; direct files each expose independent controls')
        await page.fill('#event-track-volume-1-number','65')
        # Mobile arrow reorder retains the metadata with the track, not with the old index.
        await page.locator('.event-order-actions').nth(1).get_by_role('button',name='아래로',exact=False).click()
        assert await page.input_value('#event-track-volume-2-number')=='65'
        assert await page.input_value('#event-track-volume-1-number')=='160'
        checks.append('reordering keeps gain attached to the right audio file')
        await page.set_viewport_size({'width':1500,'height':1050})
        await page.evaluate("document.querySelector('#toast').hidden=true")
        await page.locator('#event-track-volume-1').scroll_into_view_if_needed()
        before=await page.evaluate('eventDraft.tracks.map(eventTrackKey)')
        box=await page.locator('#event-track-volume-1').bounding_box()
        await page.mouse.move(box['x']+box['width']*.7,box['y']+box['height']/2)
        await page.mouse.down();await page.mouse.move(box['x']+box['width']*.4,box['y']+box['height']/2,steps=8);await page.mouse.up()
        assert await page.evaluate('eventDraft.tracks.map(eventTrackKey)')==before
        assert await page.evaluate('eventDraft.tracks[1].volumePercent')!=160
        checks.append('real pointer drag on range changes gain without dragging/reordering the row')
        await page.fill('#event-track-volume-1-number','125')
        for width in [320,360,390,768,1500]:
            await page.set_viewport_size({'width':width,'height':1000})
            assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),width
            assert not await page.locator('#event-editor-dialog').evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),width
            for row in await page.locator('.track-volume-control:visible').all():
                assert not await row.evaluate('(e)=>e.scrollWidth>e.clientWidth+1'),width
        await page.locator('#event-draft-list').scroll_into_view_if_needed();await page.screenshot(path=str(out/'event-volume-desktop.png'),full_page=False)
        await page.set_viewport_size({'width':390,'height':900});await page.locator('#event-track-volume-1-control').scroll_into_view_if_needed()
        await page.screenshot(path=str(out/'event-volume-mobile.png'),full_page=False)
        checks.append('event gain controls and long filenames fit 320/360/390/768/1500px widths')
        before=await page.evaluate("savedRequests.filter(r=>r.path==='/api/events'&&r.method==='PUT').length")
        await page.fill('#event-track-volume-1-number','999');await page.click('#save-event-draft')
        assert await page.evaluate("savedRequests.filter(r=>r.path==='/api/events'&&r.method==='PUT').length")==before
        await page.fill('#event-track-volume-1-number','125');await page.click('#save-event-draft')
        await page.locator('#event-editor-dialog').wait_for(state='hidden')
        saved=await page.evaluate("savedRequests.filter(r=>r.path==='/api/events'&&r.method==='PUT').at(-1).data")
        assert saved['tracks'][1]['volumePercent']==125 and saved['tracks'][2]['volumePercent']==65
        assert 'volumePercent' not in saved['tracks'][0]
        checks.append('event validation blocks invalid gain; save includes only raw-track levels, no double library gain')
        await page.locator('[data-event-key=out]').get_by_role('button',name='곡 · 순서 편집',exact=True).click()
        await page.fill('#event-default-volume-number','135');await page.click('#save-event-draft')
        await page.locator('#event-editor-dialog').wait_for(state='hidden')
        saved=await page.evaluate("savedRequests.filter(r=>r.path==='/api/events'&&r.method==='PUT').at(-1).data")
        assert saved['volumePercent']==135 and 'tracks' not in saved
        assert await page.evaluate("fixture.events.find(e=>e.key==='out').custom.file==='out.mp3'")
        checks.append('gain-only edit preserves a legacy named sound file instead of replacing it')
        await page.click('[data-tab=songs]')
        for role in ['player','user']:
            await page.evaluate('(r)=>{currentUser.role=r;applyRole();renderSongs();renderEvents();}',role)
            assert await page.locator('.song-editor').is_hidden()
            assert await page.locator('.song-row').get_by_role('button',name='수정',exact=True).count()==0
            assert await page.locator('.event-card').get_by_role('button',name='곡 · 순서 편집',exact=True).count()==0
        checks.append('play-only and user roles cannot access gain editing controls')
        await page.evaluate("currentUser.role='registrar';applyRole();state.nowPlaying={title:'김선수 등장곡',status:'playing',songVolumePercent:145,masterVolumePercent:50};drawLive(state)")
        assert '곡 145% × 봇 50%' in await page.locator('#playing-status').inner_text()
        checks.append('now-playing display distinguishes track gain from master volume')
        await page.evaluate('showLogin()');assert await page.input_value('#song-volume-number')=='100'
        checks.append('logout clears editor gain and private editing context')
        assert not errors,errors
        report={'passed':len(checks),'checks':checks,'pageErrors':errors,'scope':'Chromium DOM and native input/drag events with mock API, not live Discord or phone install'}
        (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
        await browser.close()

if __name__=='__main__':asyncio.run(main())
