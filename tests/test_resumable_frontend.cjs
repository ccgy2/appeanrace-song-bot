'use strict';
// Exercise the actual shipped state machine with a virtual 10GB File. No large
// allocation, network, browser, account or server credentials are used here.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const crypto=require('node:crypto').webcrypto;
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,`Missing code: ${start}`);return source.slice(a,b);}
const chunk=4*1024*1024,limit=10240*1024*1024;
const engineContext=vm.createContext({AbortController,setTimeout,clearTimeout,Date,crypto});
vm.runInContext(between('const UPLOAD_CHUNK_BYTES=','function uploadSnapshot()'),engineContext);
const upload=engineContext.uploadChunkedFile;
const virtualFile=(size,name='경기 영상.mp4')=>{
  const slices=[];
  return {name,size,slices,
    arrayBuffer(){throw new Error('Never read the whole file');},
    slice(start,end){assert(start>=0&&end<=size&&end>start);const value={start,end,size:end-start};slices.push(value);return value;},
  };
};
function mockServer(file,{chunkSize=chunk,failChunk,processingPolls=0}={}){
  let offset=0,status='uploading',polls=0,inFlight=0,maxInFlight=0;
  const calls=[],progress=[],delays=[],id='upload-1',asset={id:'a'.repeat(32),name:file.name,duration:12,bytes:file.size};
  const state=()=>({id,status,offset,size:file.size,chunkSize,...(status==='complete'?{result:asset}:{})});
  const send=async(url,method,data,options={})=>{
    inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);calls.push({url,method,data,options});
    try{
      await Promise.resolve();
      if(url==='/api/uploads'&&method==='POST'){assert.equal(data.size,file.size);assert.equal(data.filename,file.name);return state();}
      if(method==='GET'){
        if(status==='processing'&&++polls>=processingPolls)status='complete';
        return state();
      }
      if(method==='PUT'){
        const requested=Number(new URL(url,'https://local.test').searchParams.get('offset'));
        assert.equal(requested,offset,'client resumes at the committed server offset');
        assert.equal(data.start,offset);assert.ok(data.size<=chunk);assert.equal(options.raw,true);
        options.onProgress?.(Math.floor(data.size/2));
        if(failChunk){const result=failChunk({offset,data,commit:()=>{offset+=data.size;},calls});if(result)throw result;}
        offset+=data.size;options.onProgress?.(data.size);return state();
      }
      if(method==='POST'&&url.endsWith('/complete')){assert.equal(offset,file.size);status=processingPolls?'processing':'complete';return state();}
      throw new Error('Unexpected request '+method+' '+url);
    }finally{inFlight--;}
  };
  return {send,calls,progress,delays,asset,state,maxInFlight:()=>maxInFlight,
    options:{send,onProgress:value=>progress.push(value),sleep:async ms=>{delays.push(ms);},now:()=>Date.now()},
    setOffset:value=>{offset=value;},setStatus:value=>{status=value;},
  };
}
function transient(message='lost response',status=0){const error=new Error(message);error.retryable=true;error.status=status;return error;}

test('a 10GB MP4 is streamed as 2560 sequential 4MiB slices, never as a whole-file body',async()=>{
  const file=virtualFile(limit),server=mockServer(file);
  const result=await upload(file,server.options);
  assert.equal(result.id,server.asset.id);
  assert.equal(file.slices.length,2560);
  assert.equal(server.maxInFlight(),1);
  assert.equal(server.calls.filter(call=>call.method==='PUT').length,2560);
  assert.equal(file.slices[0].start,0);assert.equal(file.slices.at(-1).end,limit);
  assert.equal(server.progress.at(-1).stage,'complete');assert.equal(server.progress.at(-1).loaded,limit);
  assert.ok(server.calls.every(call=>call.data!==file));
});

test('a non-multiple tail is sent exactly once and progress is based on bytes',async()=>{
  const file=virtualFile(chunk+777),server=mockServer(file);
  await upload(file,server.options);
  assert.deepEqual(file.slices.map(value=>value.size),[chunk,777]);
  const midway=server.progress.find(value=>value.loaded===chunk/2);
  assert.ok(midway);assert.equal(midway.percent,(chunk/2)/file.size*100);
  assert.equal(server.progress.at(-1).percent,100);
});

test('configured limits, empty files and unsupported extensions fail before any network request',async()=>{
  for(const file of [virtualFile(limit+1),virtualFile(0),virtualFile(10,'payload.exe')]){
    const server=mockServer(file);
    await assert.rejects(upload(file,server.options));assert.equal(server.calls.length,0);
  }
  const smaller=virtualFile(2*chunk),server=mockServer(smaller);
  await assert.rejects(upload(smaller,{...server.options,maxBytes:chunk}),/최대/);
  assert.equal(server.calls.length,0);
});

test('uppercase MP4 is accepted and advertised larger chunks cannot exceed the 4MiB client bound',async()=>{
  const file=virtualFile(chunk*2+1,'MATCH.MP4'),server=mockServer(file,{chunkSize:128*1024*1024});
  await upload(file,{...server.options,chunkBytes:128*1024*1024});
  assert.ok(file.slices.every(slice=>slice.size<=chunk));
});

test('invalid chunk configuration falls back to a finite positive slice size',async()=>{
  for(const chunkBytes of [0,-1,NaN]){
    const file=virtualFile(5),server=mockServer(file);
    await upload(file,{...server.options,chunkBytes});assert.equal(file.slices.length,1);
  }
});

test('a lost response after committing a chunk queries offset and skips the already-saved bytes',async()=>{
  let failed=false;
  const file=virtualFile(chunk*2+5),server=mockServer(file,{failChunk:({offset,commit})=>{
    if(!failed&&offset===0){failed=true;commit();return transient();}
  }});
  await upload(file,server.options);
  assert.deepEqual(file.slices.map(value=>value.start),[0,chunk,2*chunk]);
  const requests=server.calls.map(call=>call.method);
  assert.deepEqual(requests.slice(0,4),['POST','PUT','GET','PUT']);
  assert.ok(server.progress.some(value=>value.stage==='retrying'));
});

test('a lost start response retries the same secure request id without creating a new reservation',async()=>{
  const file=virtualFile(11),server=mockServer(file);let first=true;const keys=new Set(),base=server.send;
  const send=async(...args)=>{
    if(args[0]==='/api/uploads'){
      assert.match(args[2].requestId,/^[a-f0-9]{32}$/);keys.add(args[2].requestId);
      const result=await base(...args);if(first){first=false;throw transient('lost create response');}return result;
    }
    return base(...args);
  };
  await upload(file,{...server.options,send});
  assert.equal(keys.size,1);assert.equal(server.calls.filter(call=>call.url==='/api/uploads').length,2);
  assert.equal(file.slices.length,1);
});

test('exhausted start retries keep the request id for manual retry even without an upload id',async()=>{
  const file=virtualFile(11),keys=[],server=mockServer(file);
  let error;try{await upload(file,{...server.options,send:async(url,method,data)=>{keys.push(data.requestId);throw transient();}});}catch(value){error=value;}
  assert.equal(keys.length,6);assert.equal(new Set(keys).size,1);assert.equal(error.uploadId,'');assert.equal(error.requestId,keys[0]);assert.equal(error.resumeAllowed,true);
  await upload(file,{...server.options,requestId:error.requestId});
  assert.equal(server.calls[0].data.requestId,error.requestId);
});

test('a failed chunk with no committed bytes resends only that bounded slice',async()=>{
  let failed=false;
  const file=virtualFile(chunk+5),server=mockServer(file,{failChunk:()=>{
    if(!failed){failed=true;return transient('offset conflict',409);}
  }});
  await upload(file,server.options);
  assert.deepEqual(file.slices.map(value=>value.start),[0,0,chunk]);
});

test('repeated transient failures stop after bounded retries and retain the upload id for manual resume',async()=>{
  const file=virtualFile(chunk+5),server=mockServer(file,{failChunk:()=>transient()});
  let error;try{await upload(file,server.options);}catch(value){error=value;}
  assert.ok(error);assert.equal(error.resumeAllowed,true);assert.equal(error.uploadId,'upload-1');
  assert.equal(server.calls.filter(call=>call.method==='PUT').length,6);
  assert.equal(server.delays.length,5);
  assert.ok(!server.calls.some(call=>call.url.endsWith('/complete')));
});

test('manual resume starts with GET and sends only bytes after the committed offset',async()=>{
  const file=virtualFile(chunk*2+3),server=mockServer(file);server.setOffset(chunk);
  await upload(file,{...server.options,resumeId:'upload-1'});
  assert.equal(server.calls[0].method,'GET');
  assert.deepEqual(file.slices.map(value=>value.start),[chunk,chunk*2]);
  assert.equal(server.calls.filter(call=>call.url==='/api/uploads').length,0);
});

test('already-complete resumable status returns the same asset without reuploading',async()=>{
  const file=virtualFile(chunk),server=mockServer(file);server.setOffset(file.size);server.setStatus('complete');
  const result=await upload(file,{...server.options,resumeId:'upload-1'});
  assert.equal(result.id,server.asset.id);assert.equal(file.slices.length,0);assert.equal(server.calls.length,1);
});

test('processing polls do not report complete until server audio validation succeeds',async()=>{
  const file=virtualFile(17),server=mockServer(file,{processingPolls:3});
  await upload(file,server.options);
  assert.equal(server.calls.filter(call=>call.method==='GET').length,3);
  assert.ok(server.progress.filter(value=>value.stage==='processing').length>=3);
  assert.equal(server.progress.filter(value=>value.stage==='complete').length,1);
  assert.equal(server.progress.at(-1).stage,'complete');
});

test('lost completion response is recovered from status without creating a second asset',async()=>{
  const file=virtualFile(12),server=mockServer(file);const base=server.send;
  const send=async(...args)=>{const result=await base(...args);if(args[0].endsWith('/complete'))throw transient();return result;};
  const result=await upload(file,{...server.options,send});
  assert.equal(result.id,server.asset.id);
  assert.equal(server.calls.filter(call=>call.url.endsWith('/complete')).length,1);
});

test('terminal processing errors never return success or offer resume for invalid media',async()=>{
  const file=virtualFile(12),server=mockServer(file);const base=server.send;
  const send=async(...args)=>{const result=await base(...args);return args[0].endsWith('/complete')?{...result,status:'error',error:'오디오 트랙이 없습니다.'}:result;};
  await assert.rejects(upload(file,{...server.options,send}),error=>error.message.includes('오디오 트랙')&&error.resumeAllowed===false);
  assert.ok(!server.progress.some(value=>value.stage==='complete'));
});

test('invalid size, offset or upload identity is rejected before it can corrupt resume state',async()=>{
  for(const change of [state=>({...state,size:123456}),state=>({...state,offset:-1}),state=>({...state,offset:1.5}),state=>({...state,status:'unknown'})]){
    const file=virtualFile(12),server=mockServer(file);
    await assert.rejects(upload(file,{...server.options,send:async(...args)=>change(await server.send(...args))}),/상태 응답/);
    assert.equal(file.slices.length,0);
  }
});

test('abort before starting sends nothing; abort during recovery prevents any new request',async()=>{
  const controller=new AbortController();controller.abort();
  const file=virtualFile(12),server=mockServer(file);
  await assert.rejects(upload(file,{...server.options,signal:controller.signal}),error=>error.name==='AbortError');
  assert.equal(server.calls.length,0);
  const controller2=new AbortController(),server2=mockServer(file,{failChunk:()=>transient()});
  await assert.rejects(upload(file,{...server2.options,signal:controller2.signal,sleep:async()=>{controller2.abort();}}),error=>error.name==='AbortError');
  assert.deepEqual(server2.calls.map(call=>call.method),['POST','PUT']);
});

function contextGuardFixture(){
  const context=vm.createContext({canEditMusic:()=>true,csrf:'csrf',accessToken:'token',apiBase:'https://bot.example',selectedTeam:'A팀',selectedGuild:'guild',selectedBot:'primary',currentCategory:'entrance',uploadEpoch:4});
  vm.runInContext(between('function uploadSnapshot()','function uploadTransport('),context);
  return context;
}
test('upload context invalidates on account, CSRF, API, team, guild, bot, category and reset changes',()=>{
  for(const [key,value] of [['accessToken','other'],['csrf','other'],['apiBase','https://other.example'],['selectedTeam','B팀'],['selectedGuild','other'],['selectedBot','secondary'],['currentCategory','cheer'],['uploadEpoch',5]]){
    const context=contextGuardFixture(),snapshot=context.uploadSnapshot();assert.equal(context.currentUploadSnapshot(snapshot),true);
    context[key]=value;assert.equal(context.currentUploadSnapshot(snapshot),false,key);
  }
  const context=contextGuardFixture(),snapshot=context.uploadSnapshot();context.canEditMusic=()=>false;assert.equal(context.currentUploadSnapshot(snapshot),false);
});

function wrapperFixture(file,server){
  const calls=[],context=vm.createContext({AbortController,setTimeout,clearTimeout,Date,crypto,
    canEditMusic:()=>true,csrf:'csrf',accessToken:'token',apiBase:'https://bot.example',selectedTeam:'A팀',selectedGuild:'guild',selectedBot:'primary',currentCategory:'entrance',
    state:{maxUploadMb:10240,uploadChunkBytes:chunk,fileStorage:{uploadsAllowed:true}},
  });
  vm.runInContext(between('const UPLOAD_CHUNK_BYTES=','function uploadTransport('),context);
  context.uploadTransport=async(url,method,data,options,snapshot)=>{
    calls.push({url,method,snapshot});
    if(method==='DELETE')return {ok:true};
    return server.send(url,method,data,options);
  };
  vm.runInContext(between('function discardUpload(','async function selectSongFile('),context);
  return {context,calls};
}
test('the upload wrapper refuses unauthorized roles before creating an upload session',async()=>{
  const file=virtualFile(5),server=mockServer(file),f=wrapperFixture(file,server);
  f.context.canEditMusic=()=>false;
  await assert.rejects(f.context.uploadFile(file),/권한/);
  assert.equal(f.calls.length,0);
});

test('a team or replacement-login change during completion rejects the stale asset and cancels its stage',async()=>{
  for(const key of ['selectedTeam','accessToken']){
    const file=virtualFile(5),server=mockServer(file),f=wrapperFixture(file,server),send=f.context.uploadTransport;
    f.context.uploadTransport=async(...args)=>{const result=await send(...args);if(args[0].endsWith('/complete'))f.context[key]='replacement';return result;};
    await assert.rejects(f.context.uploadFile(file),error=>error.name==='AbortError');
    assert.ok(f.calls.some(call=>call.method==='DELETE'));
    assert.ok(f.calls.every(call=>call.snapshot.token==='token'),'cleanup stays bound to the original session');
  }
});

test('a reset aborts a currently pending chunk and no completion request follows',async()=>{
  const file=virtualFile(chunk+1),server=mockServer(file),f=wrapperFixture(file,server),base=f.context.uploadTransport;
  let chunkStarted;const started=new Promise(resolve=>{chunkStarted=resolve;});
  f.context.uploadTransport=(url,method,data,options,snapshot)=>{
    if(method!=='PUT')return base(url,method,data,options,snapshot);
    chunkStarted();
    return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=new Error('cancelled');error.name='AbortError';reject(error);},{once:true}));
  };
  const pending=f.context.uploadFile(file);await started;
  f.context.cancelUploadJobs();
  await assert.rejects(pending,error=>error.name==='AbortError');
  assert.ok(f.calls.some(call=>call.method==='DELETE'));
  assert.ok(!f.calls.some(call=>call.url.endsWith('/complete')));
});

function mediaFixture(bytes){
  let blobCalls=0;const calls=[],scope={value:'session'};
  const id='a'.repeat(32),context=vm.createContext({apiBase:'https://bot.example',location:{origin:'https://site.example'},URL,mediaScope:()=>scope.value,
    privateMediaBlob:async()=>{blobCalls++;return {size:bytes};},
    api:async(path,method,data)=>{calls.push({path,method,data});return {url:'/api/media/'+id+'?ticket=opaque'+(data.download?'&download=1':''),bytes};},
  });
  vm.runInContext(between('const SMALL_MEDIA_BYTES =','async function attachMedia('),context);
  return {context,id,calls,scope,blobCalls:()=>blobCalls};
}
test('large and unknown media sizes always use scoped native tickets, never a Blob download',async()=>{
  for(const bytes of [limit,undefined,NaN]){
    const f=mediaFixture(bytes);const result=await f.context.privateMediaSource(f.id,bytes,true);
    assert.equal(f.blobCalls(),0);assert.equal(f.calls.length,1);
    assert.equal(f.calls[0].path,'/api/media/'+f.id+'/ticket');assert.equal(f.calls[0].data.download,true);
    assert.ok(result.url.startsWith('https://bot.example/api/media/'));assert.equal(result.blob,undefined);
  }
});

test('small known media retains private cache path, while hostile ticket URLs are rejected',async()=>{
  const f=mediaFixture(1024);assert.ok((await f.context.privateMediaSource(f.id,1024)).blob);assert.equal(f.blobCalls(),1);assert.equal(f.calls.length,0);
  for(const url of ['https://attacker.example/file','//attacker.example/api/media/'+f.id+'?ticket=x','/api/media/other?ticket=x','/api/media/'+f.id+'?wrong=x','/api/media/'+f.id+'?ticket=x#secret']){
    assert.throws(()=>f.context.checkedMediaTicketUrl(f.id,url),/주소/);
  }
});

test('a late media ticket cannot attach itself to a replacement login',async()=>{
  const f=mediaFixture(limit);
  f.context.api=async()=>{f.scope.value='new-session';return {url:'/api/media/'+f.id+'?ticket=old'};};
  await assert.rejects(f.context.privateMediaSource(f.id,limit),/로그인 정보/);
});

test('full audio backups use a native ticket link without creating a ZIP Blob in memory',async()=>{
  const calls=[],links=[],context=vm.createContext({URL,apiBase:'https://bot.example',location:{origin:'https://site.example'},csrf:'csrf',mediaScope:()=> 'session',
    api:async(path,method,data)=>{calls.push({path,method,data});return {url:'/api/backup?ticket=opaque&download=1'};},
    request:()=>{throw new Error('Large backup must not be read as a Blob');},
    document:{createElement:()=>{const link={click(){this.clicked=true;},remove(){}};links.push(link);return link;},body:{append(){}}},
  });
  vm.runInContext(between('const SMALL_MEDIA_BYTES =','async function exportBackup()'),context);
  await context.downloadFile('/api/backup','backup.zip');
  assert.equal(calls[0].path,'/api/backup/ticket');assert.equal(calls[0].method,'POST');
  assert.equal(links[0].href,'https://bot.example/api/backup?ticket=opaque&download=1');assert.equal(links[0].clicked,true);
});

test('both deployment copies contain the same upload UI and MP4 input support',()=>{
  for(const name of ['app.js','index.html','style.css'])assert.equal(fs.readFileSync(path.join(root,'static',name),'utf8'),fs.readFileSync(path.join(root,'웹사이트/static',name),'utf8'),name);
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
  assert.match(html,/id="song-file"[^>]*accept="[^"]*\.mp4/);
  assert.ok(html.includes('id="cancel-song-upload"'));assert.ok(html.includes('id="retry-song-upload"'));
});
