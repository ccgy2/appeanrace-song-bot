'use strict';
// Status labels are derived from the shipped rendering code. No Discord,
// browser, media device or network connection is required.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const start=source.indexOf('function playbackPresentation(');
const end=source.indexOf('\nasync function loadLive()',start);
assert.ok(start>=0&&end>start,'The shipped playback presentation and renderer are present');
function state(patch={}){
  const initial={ready:true,team:'A팀',botLabel:'청팀 봇',state:{volume:.5},
    voice:{connected:true,playing:true,paused:false,channelName:'야구방',latencyMs:12},
    nowPlaying:{title:'시험 곡',status:'playing',team:'A팀',masterVolumePercent:50,songVolumePercent:100},
  };
  return {...initial,...patch,voice:{...initial.voice,...patch.voice},nowPlaying:{...initial.nowPlaying,...patch.nowPlaying}};
}
function fixture(){
  const nodes=new Map();
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{textContent:'',value:'',title:'',attributes:{},classList:{toggle(){}},
      setAttribute(key,value){this.attributes[key]=value;}});
    return nodes.get(selector);
  };
  const context=vm.createContext({$:node,selectedTeam:'A팀',selectedBot:'primary',document:{activeElement:null},
    statusNames:{preparing:'오디오 준비 중',playing:'재생 중',finished:'재생 완료',stopped:'정지됨',error:'재생 오류'},
    drawAutoEntrance(){},canPlay:()=>true,api(){throw new Error('Drawing status must not change saved settings');},
  });
  vm.runInContext(source.slice(start,end),context);
  return {context,node,show:value=>context.playbackPresentation(value)};
}

test('same-team live master 0 overrides stale 50% and explicitly shows mute',()=>{
  const f=fixture(),s=state({nowPlaying:{masterVolumePercent:0}});
  f.context.drawLive(s);
  assert.equal(Number(f.node('#volume').value),0);
  assert.equal(f.node('#volume-value').textContent,'0%');
  assert.match(f.node('#playing-status').textContent,/전체 볼륨 0%/);
  assert.doesNotMatch(f.node('#playing-status').textContent,/봇 재생 중/);
  assert.equal(s.state.volume,.5,'rendering must not overwrite saved volume');
});

test('same-team live nonzero gain follows a change from another controller',()=>{
  const f=fixture(),p=f.show(state({nowPlaying:{masterVolumePercent:23}}));
  assert.equal(p.volumePercent,23);assert.match(p.statusText,/봇 재생 중/);
});

test('zero song gain is visible even when the master slider is nonzero',()=>{
  const p=fixture().show(state({nowPlaying:{songVolumePercent:0}}));
  assert.equal(p.volumePercent,50);assert.match(p.statusText,/곡 자체 볼륨 0%/);
  assert.doesNotMatch(p.statusText,/봇 재생 중/);
});

test('all zero-gain causes remain visible together',()=>{
  const p=fixture().show(state({nowPlaying:{masterVolumePercent:0,songVolumePercent:0}}));
  assert.match(p.statusText,/전체 볼륨 0%/);assert.match(p.statusText,/곡 자체 볼륨 0%/);
});

test('server mute, self mute and stage audience are reported instead of normal playback',()=>{
  for(const [field,text] of [['serverMuted','Discord 서버 음소거'],['selfMuted','봇 자체 음소거'],['stageAudience','스테이지 청중 상태']]){
    const p=fixture().show(state({voice:{[field]:true}}));
    assert.ok(p.statusText.includes(text),field);assert.ok(p.voiceDetail.includes(text),field);
    assert.doesNotMatch(p.statusText,/봇 재생 중/);assert.doesNotMatch(p.voiceDetail,/정상/);
  }
});

test('a paused voice client is shown as paused despite a cached playing status',()=>{
  const p=fixture().show(state({voice:{playing:false,paused:true}}));
  assert.match(p.statusText,/일시정지/);assert.doesNotMatch(p.statusText,/봇 재생 중/);
});

test('disconnected voice cannot be rendered as still playing',()=>{
  const p=fixture().show(state({voice:{connected:false,playing:false}}));
  assert.match(p.statusText,/통화방 연결 끊김/);assert.doesNotMatch(p.statusText,/봇 재생 중/);
  assert.match(p.voiceDetail,/통화방을 선택/);
});

test('connected but inactive voice exposes stale playing state',()=>{
  const p=fixture().show(state({voice:{playing:false}}));
  assert.match(p.statusText,/재생 확인 필요/);assert.doesNotMatch(p.statusText,/봇 재생 중/);
});

test('preparation before connecting is distinguished from playback',()=>{
  const p=fixture().show(state({nowPlaying:{status:'preparing'},voice:{connected:false,playing:false}}));
  assert.match(p.statusText,/오디오 준비 중/);assert.match(p.statusText,/통화방 연결 대기/);
  assert.doesNotMatch(p.statusText,/봇 재생 중/);
});

test('another team keeps its selected gain and explicitly scopes the control',()=>{
  const f=fixture(),s=state({team:'B팀',state:{volume:.65},nowPlaying:{team:'A팀',masterVolumePercent:0}});
  f.context.selectedTeam='B팀';f.context.drawLive(s);
  assert.equal(Number(f.node('#volume').value),65);
  const status=f.node('#playing-status').textContent;
  assert.match(status,/재생 팀 A팀/);assert.match(status,/전체 볼륨 0%/);
  assert.match(status,/볼륨 조절은 선택한 B팀에 적용됩니다/);
  assert.equal(f.node('#volume-caption').textContent,'선택팀 볼륨');
  assert.match(f.node('#volume').attributes['aria-label'],/B팀 볼륨/);
  assert.match(f.node('#volume').attributes['aria-label'],/현재 재생 팀 A팀의 볼륨은 바뀌지 않습니다/);
  assert.equal(s.state.volume,.65);assert.equal(s.nowPlaying.masterVolumePercent,0);
});

test('normal status identifies the actual bot, channel and playing team without delivery guarantees',()=>{
  const p=fixture().show(state());
  assert.match(p.statusText,/청팀 봇/);assert.match(p.statusText,/야구방/);assert.match(p.statusText,/재생 팀 A팀/);
  assert.doesNotMatch(p.statusText,/전송 완료|들립니다|청취 확인|정상 재생/);
});

test('a finished track does not leave old zero-gain warnings or override selected settings',()=>{
  const p=fixture().show(state({voice:{playing:false},nowPlaying:{status:'finished',masterVolumePercent:0,songVolumePercent:0}}));
  assert.equal(p.volumePercent,50);assert.match(p.statusText,/재생 완료/);
  assert.doesNotMatch(p.statusText,/0%|소리가 나오지 않는 설정/);
});

test('detailed decoder errors remain visible',()=>{
  const p=fixture().show(state({voice:{playing:false},nowPlaying:{status:'error',error:'오디오 데이터를 읽지 못했습니다.'}}));
  assert.match(p.statusText,/오디오 데이터를 읽지 못했습니다/);assert.doesNotMatch(p.statusText,/봇 재생 중/);
});

test('legacy missing gain falls back to selected-team volume without NaN output',()=>{
  const p=fixture().show(state({state:{volume:0},nowPlaying:{masterVolumePercent:undefined,songVolumePercent:undefined}}));
  assert.equal(p.volumePercent,0);assert.match(p.statusText,/전체 볼륨 0%/);
  const malformed=fixture().show(state({state:{volume:Infinity},nowPlaying:{masterVolumePercent:NaN}}));
  assert.equal(malformed.volumePercent,50);assert.doesNotMatch(malformed.statusText,/NaN|Infinity/);
});

test('an actively dragged slider is not overwritten by polling, while mute status still updates',()=>{
  const f=fixture();f.node('#volume').value=37;f.node('#volume-value').textContent='37%';
  f.context.document.activeElement=f.node('#volume');
  f.context.drawLive(state({nowPlaying:{masterVolumePercent:0}}));
  assert.equal(Number(f.node('#volume').value),37);assert.equal(f.node('#volume-value').textContent,'37%');
  assert.match(f.node('#playing-status').textContent,/전체 볼륨 0%/);
});

test('both deployment copies have the same playback renderer and accessible status labels',()=>{
  assert.equal(source,fs.readFileSync(path.join(root,'웹사이트/static/app.js'),'utf8'));
  for(const directory of ['static','웹사이트/static']){
    const html=fs.readFileSync(path.join(root,directory,'index.html'),'utf8');
    assert.match(html,/id="playing-status"[^>]*role="status"[^>]*aria-live="polite"/);
    assert.ok(html.includes('id="volume-caption"'));
  }
});
