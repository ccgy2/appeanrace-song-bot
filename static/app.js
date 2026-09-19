'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
let csrf = '', state = null, selectedTeam = '', selectedGuild = '';
let source = 'youtube', assetId = '', uploading = false, toastTimer, previewAudio;
let stateTicket = 0, uploadTicket = 0, previewTicket = 0;
let accessToken = '', apiBase = '', configError = '';
const mediaUrls = new Map();
const statusNames = {preparing:'오디오 준비 중',playing:'재생 중',finished:'재생 완료',stopped:'정지됨',error:'재생 오류'};

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  const node = $('#toast'); node.textContent = message;
  node.classList.toggle('error', error); node.hidden = false;
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 10000 : 4500);
}
function tokenKey() { return 'appearance:session:v2:' + (apiBase || location.origin); }
function normalizeApiBase(raw) {
  const u = new URL(String(raw || '').trim());
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(u.hostname);
  if ((u.protocol !== 'https:' && !(local && u.protocol === 'http:')) || u.username || u.password || (u.pathname !== '/' && u.pathname !== '') || u.search || u.hash) throw new Error('Railway 주소는 경로 없는 HTTPS 주소여야 합니다.');
  if (location.protocol === 'https:' && u.protocol !== 'https:') throw new Error('Firebase에서는 HTTPS Railway 주소만 사용할 수 있습니다.');
  if (location.hostname === 'appearance-song.web.app' && !u.hostname.endsWith('.up.railway.app')) throw new Error('Railway의 Public Networking 주소(…up.railway.app)를 입력하세요.');
  return u.origin;
}
function rememberToken(token) {
  accessToken = token;
  try { if (token) sessionStorage.setItem(tokenKey(), token); else sessionStorage.removeItem(tokenKey()); } catch { /* Memory-only in restricted browsers. */ }
}
function releaseAudio(audio) {
  if (!audio) return;
  audio.pause(); audio.removeAttribute('src');
  const previous = mediaUrls.get(audio);
  if (previous) { URL.revokeObjectURL(previous); mediaUrls.delete(audio); }
}
function clearMedia() { for (const audio of Array.from(mediaUrls.keys())) releaseAudio(audio); }
function showLogin() {
  csrf = ''; state = null; stateTicket++; previewTicket++; uploadTicket++;
  rememberToken(''); clearMedia();
  $('#app').hidden = true; $('#login-screen').hidden = false;
  $('#password').value = '';
  resetEditor();
}
function apiUrl(path) {
  if (configError) throw new Error(configError);
  if (!path.startsWith('/api/') && path !== '/healthz') throw new Error('잘못된 API 경로입니다.');
  return apiBase + path;
}
function configureClient() {
  configError = '';
  try {
    const configured = String(window.APPEARANCE_CONFIG?.API_BASE_URL || '').trim();
    const raw = configured;
    if (raw) apiBase = normalizeApiBase(raw);
    else if (/\.(web\.app|firebaseapp\.com)$/.test(location.hostname)) configError = '웹 연결 설정이 비어 있습니다. 최신 웹사이트 파일로 다시 배포하세요.';
    else if (!['http:', 'https:'].includes(location.protocol)) configError = '파일을 직접 열지 말고 Firebase Hosting 또는 로컬 웹 서버로 접속하세요.';
  } catch (err) {
    configError = err.message || 'Railway 공개 HTTPS 주소를 확인하세요.';
    apiBase = '';
  }
  try { accessToken = sessionStorage.getItem(tokenKey()) || ''; } catch { accessToken = ''; }
  $('#server-address').textContent = configError ? '봇 서버 주소 설정 필요' : (apiBase || location.origin);
}

async function request(path, method = 'GET', data, asBlob = false) {
  const options = {method, credentials:'omit', cache:'no-store', headers:{}};
  if (accessToken && path !== '/api/login' && path !== '/api/connection') options.headers.Authorization = 'Bearer ' + accessToken;
  if (!['GET','HEAD'].includes(method) && csrf) options.headers['X-CSRF-Token'] = csrf;
  if (data !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
  let res;
  const url = apiUrl(path);
  const controller = new AbortController(); options.signal = controller.signal;
  const timer = setTimeout(()=>controller.abort(), asBlob ? 180000 : (path==='/api/connection' ? 15000 : 60000));
  try { res = await fetch(url, options); } catch (err) {
    throw new Error(err.name==='AbortError' ? '요청 시간이 초과됐습니다. 등록·재생은 처리됐을 수도 있으니 상태를 확인한 뒤 다시 시도하세요.' : 'Railway에 연결하지 못했습니다. 서버 실행 상태·공개 주소·WEB_ORIGINS에 현재 웹 주소가 등록됐는지 확인하세요.');
  } finally { clearTimeout(timer); }
  if (!res.ok) {
    let result; try { result = await res.json(); } catch { result = {}; }
    if (res.status === 401 && path !== '/api/login') showLogin();
    throw new Error(result.error || `요청 실패 (${res.status}). Railway 실행 로그를 확인하세요.`);
  }
  if (asBlob) return res.blob();
  try { return await res.json(); } catch { throw new Error('봇 API가 아닌 페이지가 응답했습니다. config.js의 Railway 주소와 새 서버 코드 배포 여부를 확인하세요.'); }
}
async function api(path, method = 'GET', data) { return request(path, method, data); }
async function checkConnection() {
  const node = $('#connection-status'), help = $('#connection-help');
  node.textContent = '연결 확인 중…'; node.className = ''; help.hidden = true;
  try {
    const r = await api('/api/connection');
    if (r.service !== 'appearance-song-bot' || r.apiVersion !== 2) throw new Error('Railway 봇 서버의 새 버전이 아닙니다. 이번 수정본 전체를 다시 배포하세요.');
    node.textContent = r.discordReady ? '웹 연결 정상 · 봇 온라인' : '웹 연결 정상 · 봇 미연결';
    node.className = 'success';
    if (!r.discordReady) { help.textContent = r.webOnly ? '현재 WEB_ONLY=true입니다. Railway에서 false로 바꾸세요.' : '웹 API는 연결됐습니다. Discord 토큰·봇 실행 로그를 확인하세요.'; help.hidden = false; }
    return true;
  } catch (err) {
    node.textContent = '연결 확인 필요'; node.className = 'error'; help.textContent = err.message; help.hidden = false;
    return false;
  }
}
async function attachMedia(audio, id) {
  // <audio src> cannot attach Authorization headers. Fetch privately, then use a blob URL.
  const blob = await request('/api/media/' + encodeURIComponent(id), 'GET', undefined, true);
  if (!csrf || !audio.isConnected) return false;
  releaseAudio(audio);
  const url = URL.createObjectURL(blob); mediaUrls.set(audio, url);
  audio.src = url; audio.hidden = false;
  return true;
}
async function loadEditorPreview(id, ticket) {
  const blob = await request('/api/media/' + encodeURIComponent(id), 'GET', undefined, true);
  if (ticket !== uploadTicket || !csrf) return;
  const audio = $('#upload-preview'); releaseAudio(audio);
  const url = URL.createObjectURL(blob); mediaUrls.set(audio, url); audio.src = url; audio.hidden = false;
}
async function exportBackup() {
  const blob = await request('/api/export', 'GET', undefined, true);
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = 'song-metadata-backup.json'; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function run(button, fn) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try { return await fn(); } catch (err) { toast(err.message || '처리 중 오류가 발생했습니다.', true); }
  finally { if (button) button.disabled = false; }
}
function option(value, label) { const n = el('option', label); n.value = value; return n; }
function fillSelect(select, items, value, emptyLabel) {
  select.replaceChildren();
  if (emptyLabel) select.append(option('', emptyLabel));
  for (const item of items) select.append(option(item.id, item.name));
  select.value = value || '';
}
function fmt(value) {
  const n = Math.max(0, Number(value) || 0), m = Math.floor(n / 60), sec = n % 60;
  const s = Number.isInteger(sec) ? String(sec).padStart(2, '0') : sec.toFixed(2).padStart(5, '0');
  return `${m}:${s}`;
}
function bindButton(label, cls, handler) {
  const b = el('button', label, cls); b.type = 'button';
  b.addEventListener('click', () => run(b, () => handler(b)));
  return b;
}
function context() {
  return {guildId:selectedGuild, team:selectedTeam, channelId:$('#voice-select').value || null};
}
async function control(action, extra = {}, message = '') {
  if (!selectedGuild) throw new Error('Discord 서버를 먼저 선택하세요. 연결된 서버가 없으면 진단 탭을 확인하세요.');
  const result = await api('/api/control', 'POST', {...context(), action, ...extra});
  if (message) toast(message);
  await loadState(false);
  return result;
}
function setSource(value) {
  source = value;
  $('#source-youtube').classList.toggle('selected', value === 'youtube');
  $('#source-upload').classList.toggle('selected', value === 'upload');
  $('#youtube-fields').hidden = value !== 'youtube';
  $('#upload-fields').hidden = value !== 'upload';
  $('#song-url').required = value === 'youtube';
  if (value !== 'upload') $('#upload-preview').pause();
}
function resetEditor() {
  uploadTicket++; uploading = false; assetId = '';
  $('#song-form').reset(); $('#song-name').readOnly = false;
  $('#editor-title').textContent = '새 등장곡 등록';
  $('#save-song').textContent = '＋ 등장곡 저장'; $('#save-song').disabled = false;
  $('#song-file').disabled = false; $('#upload-name').textContent = '선택된 파일 없음';
  $('#upload-progress').hidden = true;
  const audio = $('#upload-preview'); releaseAudio(audio); audio.hidden = true;
  setSource('youtube');
}
async function editSong(song) {
  resetEditor();
  $('#song-name').value = song.name; $('#song-name').readOnly = true;
  $('#song-url').value = song.url || ''; $('#song-start').value = fmt(song.start); $('#song-end').value = fmt(song.end);
  $('#member-id').value = song.memberId || ''; assetId = song.assetId || '';
  setSource(song.source || 'youtube');
  $('#editor-title').textContent = '등장곡 수정'; $('#save-song').textContent = '변경 내용 저장';
  if (assetId) {
    $('#upload-name').textContent = song.fileMissing ? '파일 없음 · 다시 업로드하세요' : (song.filename || '등록된 오디오');
    if (!song.fileMissing) await loadEditorPreview(assetId, uploadTicket);
  }
  $('#song-form').scrollIntoView({behavior:'smooth', block:'center'});
}
async function uploadFile(file, onProgress) {
  if (!file) throw new Error('오디오 파일을 선택하세요.');
  const limit = (state?.maxUploadMb || 25) * 1024 * 1024;
  if (file.size > limit) throw new Error(`파일은 최대 ${state?.maxUploadMb || 25}MB입니다.`);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open('POST',apiUrl('/api/upload')); xhr.timeout = 180000;
    xhr.withCredentials = false;
    xhr.setRequestHeader('Authorization', 'Bearer ' + accessToken);
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = (e) => { if (onProgress && e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100)); };
    xhr.onerror = () => reject(new Error('업로드 연결이 끊겼습니다. 다시 시도하세요.'));
    xhr.ontimeout = () => reject(new Error('업로드 시간이 초과됐습니다. 더 작은 파일로 시도하세요.'));
    xhr.onload = () => {
      let d; try { d = JSON.parse(xhr.responseText); } catch { reject(new Error('업로드 응답이 잘못됐습니다.')); return; }
      if (xhr.status === 401) showLogin();
      if (xhr.status >= 200 && xhr.status < 300) resolve(d);
      else reject(new Error(d.error || '파일 업로드 실패'));
    };
    const data = new FormData(); data.append('file', file); xhr.send(data);
  });
}
async function selectSongFile(file) {
  if (uploading) { toast('현재 파일을 업로드하고 있습니다.'); return; }
  const ticket = ++uploadTicket;
  uploading = true; $('#save-song').disabled = true; $('#song-file').disabled = true;
  $('#upload-progress').hidden = false; $('#upload-progress').value = 0;
  $('#upload-name').textContent = '파일 업로드 및 오디오 검사 중…';
  try {
    const result = await uploadFile(file, p => { if (ticket === uploadTicket) $('#upload-progress').value = p; });
    if (ticket !== uploadTicket) return;
    assetId = result.id;
    $('#upload-name').textContent = `${result.name} · ${fmt(result.duration)}`;
    await loadEditorPreview(assetId, ticket);
    if (ticket !== uploadTicket) return;
    $('#song-start').value = '0:00'; $('#song-end').value = fmt(Math.min(30, Math.floor(result.duration * 100) / 100));
    if (!$('#song-name').value) $('#song-name').value = file.name.replace(/\.[^.]+$/, '').slice(0,64);
    toast('파일 업로드 완료! 재생 구간을 확인하고 등장곡을 저장하세요.');
  } catch (err) {
    if (ticket === uploadTicket) { assetId=''; $('#upload-name').textContent = '업로드 실패 · 다시 선택하세요'; toast(err.message, true); }
  } finally {
    if (ticket === uploadTicket) { uploading=false; $('#save-song').disabled=false; $('#song-file').disabled=false; $('#upload-progress').hidden=true; }
  }
}
function drawLive(s) {
  const voice = s.voice;
  $('#sidebar-dot').classList.toggle('online', s.ready);
  $('#sidebar-status').textContent = s.ready ? 'Discord 연결됨' : 'Discord 미연결';
  $('#voice-title').textContent = voice?.connected ? `${voice.channelName} 연결됨` : '통화방 연결';
  $('#voice-detail').textContent = voice?.connected ? (voice.serverMuted ? '서버 음소거를 해제해주세요.' : `음성 연결 정상${voice.latencyMs !== null ? ' · ' + voice.latencyMs + 'ms' : ''}`) : '통화방을 선택하고 연결하세요.';
  $('#voice-error').hidden = !voice?.error; $('#voice-error').textContent = voice?.error || '';
  $('#playing-title').textContent = s.nowPlaying?.title || '재생 대기 중';
  $('#playing-status').textContent = s.nowPlaying?.error || statusNames[s.nowPlaying?.status] || 'Discord 통화방에서 재생됩니다';
  if (document.activeElement !== $('#volume')) { $('#volume').value = Math.round(s.state.volume * 100); $('#volume-value').textContent = $('#volume').value + '%'; }
  const off = !s.ready;
  for (const id of ['connect','disconnect','next-player','stop','activate-team']) $('#' + id).disabled = off;
  $('#volume').disabled = off;
}
async function loadState(full = true) {
  if (!csrf) return;
  const ticket = ++stateTicket;
  const params = new URLSearchParams();
  if (selectedGuild) params.set('guild_id', selectedGuild);
  if (selectedTeam) params.set('team', selectedTeam);
  const s = await api('/api/state?' + params);
  if (ticket !== stateTicket) return;
  state = s; selectedGuild = s.guildId || ''; selectedTeam = s.team;
  drawLive(s);
  if (!full) return;
  fillSelect($('#guild-select'), s.guilds, selectedGuild, s.guilds.length ? null : '연결된 서버 없음');
  fillSelect($('#team-select'), s.teams.map(t=>({id:t,name:t})), selectedTeam);
  const chosenVoice = $('#voice-select').value;
  fillSelect($('#voice-select'), s.channels.map(c=>({id:c.id,name:`${c.name} · ${c.members}명${c.available ? '' : ' (권한 부족)'}`})), chosenVoice || s.voice?.channelId || '', '통화방 선택');
  $('#storage-badge').textContent = s.storage === 'firebase' ? 'Firebase 연동' : '로컬 저장소';
  const notes = [];
  if (s.webOnly) notes.push('웹 확인 모드입니다. 실제 접속·재생은 WEB_ONLY=false로 바꾸고 봇을 실행하세요.');
  else if (!s.ready) notes.push('봇이 Discord에 연결되지 않았습니다. 토큰·실행 로그를 확인하세요. 곡 등록은 가능합니다.');
  if (s.storage === 'local') notes.push('로컬 저장 모드: 재배포 시 파일을 유지하려면 DATA_DIR에 영구 저장소가 필요합니다.');
  if (s.activeTeam !== selectedTeam) notes.push(`현재 Discord 경기 팀은 “${s.activeTeam}”입니다. 팀을 바꾸려면 “이 팀으로 경기 진행”을 누르세요.`);
  $('#workspace-note').hidden = !notes.length; $('#workspace-note').textContent = notes.join(' ');
  $('#upload-limit').textContent = `MP3 · WAV · M4A · OGG · 최대 ${s.maxUploadMb}MB`;
  renderSongs(); renderLineup(); renderEvents();
}
function renderSongs() {
  const list = $('#song-list');
  list.querySelectorAll('audio').forEach(releaseAudio); previewTicket++;
  list.replaceChildren();
  if (!state) return;
  const query = $('#search').value.trim().toLocaleLowerCase();
  $('#song-count').textContent = state.songs.length;
  const songs = state.songs.filter(s=>s.name.toLocaleLowerCase().includes(query));
  if (!songs.length) {
    const empty = el('div',null,'empty'); empty.append(el('div','♫','symbol'),el('strong',query ? '검색 결과가 없어요' : '첫 번째 등장곡을 등록해보세요'),el('p',query ? '다른 선수 이름으로 검색해보세요.' : '왼쪽 등록 폼에서 선수 이름과 노래를 추가하세요.'));
    list.append(empty); return;
  }
  for (const song of songs) {
    const row = el('article', null, 'song-row'), content = el('div');
    row.append(el('div', song.name.slice(0,1), 'song-avatar'));
    content.append(el('h4',song.name,'song-name'));
    const meta = el('div',null,'song-meta');
    meta.append(el('span',song.source === 'upload' ? 'FILE' : 'YOUTUBE','song-tag'),el('span',`${fmt(song.start)} — ${fmt(song.end)} · ${Math.round((song.end-song.start)*100)/100}초`));
    if (song.memberId) meta.append(el('span','ID 연결','song-tag'));
    if (song.fileMissing) meta.append(el('span','파일 없음','error-text'));
    content.append(meta);
    const actions = el('div',null,'song-actions');
    actions.append(bindButton('▶ 재생','soft',()=>control('play',{name:song.name},'등장곡 재생 요청을 처리했습니다.')));
    actions.append(bindButton('통화방 5초','ghost',()=>control('play',{name:song.name,preview:true})));
    actions.append(bindButton('수정','ghost',()=>editSong(song)));
    if (song.source === 'upload' && !song.fileMissing) {
      actions.append(bindButton('내 기기에서 듣기','ghost',async()=>{
        const ticket = ++previewTicket;
        if (previewAudio) releaseAudio(previewAudio);
        let audio = row.querySelector('audio');
        if (!audio) { audio = el('audio'); audio.controls=true; audio.preload='metadata'; row.append(audio); }
        if (!await attachMedia(audio, song.assetId)) return;
        if (ticket !== previewTicket) { releaseAudio(audio); return; }
        audio.currentTime = Number(song.start)||0;
        audio.ontimeupdate = ()=>{ if (audio.currentTime >= Number(song.end)) audio.pause(); };
        previewAudio=audio; await audio.play();
      }));
    } else if (song.source !== 'upload') {
      actions.append(bindButton('영상 열기','ghost',()=>{
        const u = new URL(song.url);
        if (!['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be'].includes(u.hostname) || !['http:','https:'].includes(u.protocol)) throw new Error('올바른 YouTube 주소가 아닙니다. 링크를 수정하세요.');
        u.protocol='https:'; u.searchParams.set('t',Math.floor(song.start)); window.open(u.href,'_blank','noopener,noreferrer');
      }));
    }
    actions.append(bindButton('삭제','ghost delete',async()=>{
      if (!confirm(`“${song.name}” 등장곡을 삭제할까요? 이 이름의 타순도 비워집니다. 업로드 원본 파일은 보관됩니다.`)) return;
      await api('/api/songs?'+new URLSearchParams({team:selectedTeam,name:song.name}),'DELETE');
      if ($('#song-name').value === song.name) resetEditor();
      await loadState(); toast('등장곡을 삭제했습니다.');
    }));
    content.append(actions); row.append(content); list.append(row);
  }
}
function renderLineup() {
  const grid=$('#lineup-grid'); grid.replaceChildren();
  for (let i=1;i<=9;i++) {
    const card=el('div',null,'lineup-slot'); card.classList.toggle('current',state.state.currentOrder===i);
    card.append(el('strong',String(i).padStart(2,'0')+'번'));
    const select=el('select'); select.dataset.order=i; select.setAttribute('aria-label',`${i}번 타자`);
    const names=state.songs.map(s=>s.name), current=state.lineup[i] || '';
    if (current && !names.includes(current)) names.push(current);
    fillSelect(select,names.map(n=>({id:n,name:n})),current,'타자 선택'); card.append(select);
    card.append(bindButton('▷ 등장곡 재생','ghost',()=>control('order',{order:i})));
    grid.append(card);
  }
}
function renderEvents() {
  const grid=$('#events-grid'); grid.replaceChildren();
  for (const event of state.events) {
    const card=el('article',null,'card event-card');
    card.append(el('h3',event.label));
    card.append(el('p',event.custom?.filename || event.custom?.file || `기본 효과음 ${event.bundledCount}개 · 무작위 재생`,'tiny'));
    card.append(bindButton('▶ 재생','soft',()=>control('event',{key:event.key})));
    if (event.custom) card.append(bindButton('기본 복원','ghost',async()=>{
      if (!confirm('등록한 효과음 연결을 해제하고 기본 효과음으로 돌아갈까요?')) return;
      await api('/api/events','PUT',{team:selectedTeam,key:event.key,assetId:null}); await loadState(); toast('기본 효과음으로 복원했습니다.');
    }));
    const label=el('label','오디오 파일로 교체'), input=el('input'); input.type='file'; input.accept='.mp3,.wav,.ogg,.m4a,.flac,.aac,.opus,.webm';
    input.addEventListener('change',()=>run(null,async()=>{
      const file=input.files[0]; if(!file)return;
      const team=selectedTeam;
      input.disabled=true;
      try { toast('효과음 파일을 업로드하고 있습니다.'); const result=await uploadFile(file); await api('/api/events','PUT',{team,key:event.key,assetId:result.id}); await loadState(); toast('효과음을 교체했습니다.'); }
      finally {input.disabled=false;}
    }));
    label.append(input);card.append(label);grid.append(card);
  }
}
async function diagnostics() {
  const d=await api('/api/diagnostics');
  const values=$('#diagnostic-values'); values.replaceChildren();
  const rows=[['Discord 연결',d.discordReady?'연결됨':'미연결'],['실행 모드',d.webOnly?'웹 확인 모드':'봇 + 웹'],['저장소',d.storage==='firebase'?'Firebase':'로컬 SQLite'],['FFmpeg',d.ffmpeg?'설치됨':'미설치'],['ffprobe',d.ffprobe?'설치됨':'미설치'],['Deno',d.deno?'설치됨':'미설치'],...Object.entries(d.packages),['YouTube 쿠키',d.cookiesConfigured?'설정됨':'미설정'],['웹 인증',d.authMode==='bearer'?'로그인 토큰 (교차 사이트 쿠키 불필요)':'같은 사이트 쿠키'],['Railway API',apiBase || location.origin],['Firebase 웹',d.webUrl || 'WEB_URL 미설정'],['허용된 웹 주소',(d.webOrigins || []).join(', ') || '같은 주소에서만 허용'],['업로드 제한',`${d.maxUploadMb}MB/파일 · 총 ${d.maxStorageMb}MB`]];
  for (const [k,v] of rows) { const row=el('div',null,'diagnostic-row'), strong=el('strong',v); if(['미설치','미연결'].includes(v))strong.classList.add('warn');row.append(el('span',k),strong);values.append(row); }
  const checks=$('#diagnostic-checks'); checks.replaceChildren();
  for(const text of d.checks){const row=el('div',null,'check-item');row.append(el('span','✓'),el('p',text));checks.append(row);}
}
function setTab(name) {
  $$('.nav').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  $$('.tab-panel').forEach(p=>{p.hidden=p.id!=='tab-'+name;});
  $('#page-title').textContent = {songs:'등장곡 라이브러리',lineup:'타순 관리',events:'경기 사운드',diagnostics:'연결 진단'}[name];
  if(name==='diagnostics')run(null,diagnostics);
}
$('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();const b=e.submitter;b.disabled=true;$('#login-error').textContent='';
  try{const r=await api('/api/login','POST',{password:$('#password').value,authMode:'bearer'});if(!r.accessToken)throw new Error('Railway 서버 코드를 이번 버전으로 교체하세요.');rememberToken(r.accessToken);csrf=r.csrf;$('#password').value='';$('#login-screen').hidden=true;$('#app').hidden=false;await loadState();}
  catch(err){$('#login-error').textContent=err.message;}finally{b.disabled=false;}
});
for (const id of ['logout', 'logout-mobile']) $('#' + id).addEventListener('click',()=>run($('#' + id),async()=>{await api('/api/logout','POST',{});showLogin();}));
$$('.nav').forEach(b=>b.addEventListener('click',()=>setTab(b.dataset.tab)));
$('#refresh').addEventListener('click',()=>run($('#refresh'),()=>loadState()));
$('#search').addEventListener('input',renderSongs);
$('#source-youtube').addEventListener('click',()=>setSource('youtube'));
$('#source-upload').addEventListener('click',()=>setSource('upload'));
$('#reset-song').addEventListener('click',resetEditor);
$('#song-file').addEventListener('change',()=>{if($('#song-file').files[0])selectSongFile($('#song-file').files[0]);});
const drop=$('#dropzone');
for(const name of ['dragenter','dragover'])drop.addEventListener(name,e=>{e.preventDefault();drop.classList.add('dragging');});
for(const name of ['dragleave','drop'])drop.addEventListener(name,e=>{e.preventDefault();drop.classList.remove('dragging');});
drop.addEventListener('drop',e=>{const file=e.dataTransfer.files[0];if(file)selectSongFile(file);});
$('#song-form').addEventListener('submit',e=>{
  e.preventDefault();run($('#save-song'),async()=>{
    if(uploading)throw new Error('파일 업로드가 끝난 다음 저장하세요.');
    const name=$('#song-name').value.trim();
    if(!$('#song-name').readOnly && state.songs.some(s=>s.name===name) && !confirm('같은 이름의 등장곡이 있습니다. 덮어쓸까요?'))return;
    await api('/api/songs','POST',{team:selectedTeam,name,url:$('#song-url').value,source,assetId,start:$('#song-start').value,end:$('#song-end').value,memberId:$('#member-id').value});
    resetEditor();await loadState();toast('등장곡을 저장했습니다. 바로 재생할 수 있어요.');
  });
});
$('#guild-select').addEventListener('change',()=>run(null,async()=>{selectedGuild=$('#guild-select').value;selectedTeam='';resetEditor();await loadState();}));
$('#team-select').addEventListener('change',()=>run(null,async()=>{selectedTeam=$('#team-select').value;resetEditor();await loadState();}));
$('#new-team').addEventListener('click',()=>{$('#team-name').value='';$('#team-dialog').showModal();$('#team-name').focus();});
$('#cancel-team').addEventListener('click',()=>$('#team-dialog').close());
$('#team-form').addEventListener('submit',e=>{e.preventDefault();run(e.submitter,async()=>{const team=$('#team-name').value.trim();await api('/api/team','POST',{team});selectedTeam=team;resetEditor();$('#team-dialog').close();await loadState();toast('새 팀을 추가했습니다.');});});
$('#activate-team').addEventListener('click',()=>run($('#activate-team'),async()=>{await api('/api/team','POST',{team:selectedTeam,guildId:selectedGuild,activate:true});await loadState();toast('현재 경기 팀을 변경했습니다.');}));
$('#connect').addEventListener('click',()=>run($('#connect'),()=>control('connect',{},'통화방에 연결했습니다.')));
$('#disconnect').addEventListener('click',()=>run($('#disconnect'),()=>control('disconnect',{},'통화방에서 퇴장했습니다.')));
$('#stop').addEventListener('click',()=>run($('#stop'),()=>control('stop',{},'재생을 정지했습니다.')));
$('#next-player').addEventListener('click',()=>run($('#next-player'),()=>control('next')));
$('#volume').addEventListener('input',()=>{$('#volume-value').textContent=$('#volume').value+'%';});
$('#volume').addEventListener('change',()=>run(null,()=>control('volume',{value:Number($('#volume').value)})));
$('#save-lineup').addEventListener('click',()=>run($('#save-lineup'),async()=>{const lineup={};$$('#lineup-grid select').forEach(s=>{lineup[s.dataset.order]=s.value;});await api('/api/lineup','PUT',{team:selectedTeam,lineup});await loadState();toast('타순을 저장했습니다.');}));
$('#check-connection').addEventListener('click',()=>run($('#check-connection'),checkConnection));
$('#export-backup').addEventListener('click',()=>run($('#export-backup'),exportBackup));
window.addEventListener('pagehide',clearMedia);
$('#reload-diagnostics').addEventListener('click',()=>run($('#reload-diagnostics'),diagnostics));
setInterval(()=>{if(csrf && !document.hidden)loadState(false).catch(err=>{if(csrf)toast(err.message,true);});},15000);
configureClient();
(async()=>{
  const connected=await checkConnection();
  if(!connected || !accessToken)return;
  try{const r=await api('/api/session');csrf=r.csrf;$('#login-screen').hidden=true;$('#app').hidden=false;await loadState();}
  catch(err){if(csrf)toast(err.message,true);else showLogin();}
})();
