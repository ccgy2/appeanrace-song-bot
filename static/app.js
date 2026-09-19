'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
let csrf = '', state = null, selectedTeam = '', selectedGuild = '', selectedBot = 'primary';
let source = 'youtube', assetId = '', uploading = false, toastTimer, previewAudio;
let stateTicket = 0, uploadTicket = 0, previewTicket = 0;
let accessToken = '', apiBase = '', configError = '', currentUser = null;
let currentCategory = 'entrance', editingName = null;
const categoryNames = {entrance:'등장곡', cheer:'응원가', situation:'상황별 노래'};
function categorySongs(category = currentCategory) { return state?.library?.[category] || (category === 'entrance' ? state?.songs || [] : []); }
function workspaceKey() { return 'appearance:workspace:v2:' + apiBase + ':' + (currentUser?.username || ''); }
function rememberWorkspace() {
  if (!currentUser) return;
  // Only non-secret UI choices; authentication tokens remain in sessionStorage.
  try { localStorage.setItem(workspaceKey(), JSON.stringify({team:selectedTeam, guild:selectedGuild, bot:selectedBot, category:currentCategory})); } catch {}
}
function restoreWorkspace() {
  selectedTeam=''; selectedGuild=''; selectedBot='primary'; currentCategory='entrance';
  try {
    const data=JSON.parse(localStorage.getItem(workspaceKey()) || '{}');
    if(typeof data.team==='string')selectedTeam=data.team;
    if(typeof data.guild==='string')selectedGuild=data.guild;
    if(['primary','secondary'].includes(data.bot))selectedBot=data.bot;
    if(Object.hasOwn(categoryNames,data.category))currentCategory=data.category;
  } catch {}
  resetEditor(); updateCategory();
}
function updateCategory() {
  $$('[data-category]').forEach(button=>{
    const selected=button.dataset.category===currentCategory;
    button.classList.toggle('selected',selected); button.setAttribute('aria-pressed',String(selected));
    const badge=$('#count-'+button.dataset.category); if(badge)badge.textContent=categorySongs(button.dataset.category).length;
  });
  const entrance=currentCategory==='entrance';
  $('#song-name-label').textContent=entrance?'선수 닉네임':'닉네임 / 곡 이름';
  $('#song-name').placeholder=entrance?'예: 김선수':currentCategory==='cheer'?'예: 김선수 또는 팀 응원가':'예: 홈런 축하곡';
  $('#auto-song-settings').hidden=!entrance;
  $('#category-description').textContent=entrance?'사용자 ID가 연결된 등장곡만 통화방 입장 시 자동 재생됩니다.':'별도 라이브러리에 저장됩니다. 웹의 재생 버튼으로 틀 수 있고, 상황별 노래는 관리자가 경기 사운드에 연결할 수 있습니다.';
}
function setCategory(category) {
  if(!Object.hasOwn(categoryNames,category) || category===currentCategory)return;
  currentCategory=category; resetEditor();updateCategory();renderSongs();rememberWorkspace();
}
function drawStorage(s) {
  const d=s.fileStorage;
  $('#storage-title').textContent=d?.label || '서버 업데이트 필요';
  $('#storage-message').textContent=d?.message || '이번 봇 수정본을 GitHub와 Railway에 적용해주세요.';
  $('#storage-notice').classList.toggle('unsafe',d?.status==='unsafe');
  $('#storage-notice').classList.toggle('persistent',d?.persistent===true);
  $('#song-file').disabled=uploading || d?.uploadsAllowed===false;
}

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
  if (location.protocol === 'https:' && u.protocol !== 'https:') throw new Error('HTTPS 사이트에서는 HTTPS 서버 주소만 사용할 수 있습니다.');
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
  csrf = ''; state = null; currentUser = null; stateTicket++; previewTicket++; uploadTicket++;
  rememberToken(''); clearMedia();
  $('#app').hidden = true; $('#login-screen').hidden = false;
  $('#password').value = ''; if ($('#username')) $('#username').value = '';
  selectedTeam=''; selectedGuild=''; selectedBot='primary'; currentCategory='entrance';
  setTab('songs'); updateCategory();
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
    else if (!['http:', 'https:'].includes(location.protocol)) configError = '파일을 직접 열지 말고 Railway 웹 주소 또는 로컬 웹 서버로 접속하세요.';
  } catch (err) {
    configError = err.message || 'Railway 공개 HTTPS 주소를 확인하세요.';
    apiBase = '';
  }
  try { accessToken = sessionStorage.getItem(tokenKey()) || ''; } catch { accessToken = ''; }
  $('#server-address').textContent = configError ? '봇 서버 주소 설정 필요' : (apiBase || location.origin);
}

async function request(path, method = 'GET', data, asBlob = false) {
  const options = {method, credentials:'omit', cache:'no-store', headers:{}};
  if (accessToken && path !== '/api/login' && path !== '/api/signup' && path !== '/api/connection') options.headers.Authorization = 'Bearer ' + accessToken;
  if (!['GET','HEAD'].includes(method) && csrf) options.headers['X-CSRF-Token'] = csrf;
  if (data !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
  let res;
  const url = apiUrl(path);
  const controller = new AbortController(); options.signal = controller.signal;
  const timer = setTimeout(()=>controller.abort(), asBlob ? 180000 : (path==='/api/connection' ? 15000 : 60000));
  try { res = await fetch(url, options); } catch (err) {
    throw new Error(err.name==='AbortError' ? '요청 시간이 초과됐습니다. 등록·재생은 처리됐을 수도 있으니 상태를 확인한 뒤 다시 시도하세요.' : 'Railway에 연결하지 못했습니다. Railway 배포 상태와 공개 주소를 확인하세요.');
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
    if (r.service !== 'appearance-song-bot' || r.apiVersion !== 2 || !r.capabilities?.includes('dual-bot') || !r.capabilities?.includes('event-play-modes') || !r.capabilities?.includes('separate-voice-channels')) throw new Error('Railway 봇 서버가 최신 청백전 + 경기상황 버전이 아닙니다. 이번 봇 패치를 GitHub에 반영하고 Railway를 재배포하세요.');
    const bots = Array.isArray(r.bots) ? r.bots : [];
    const status = bots.map(b=>`${b.name || (b.id==='secondary'?'백팀 봇':'청팀 봇')} ${b.ready?'온라인':'오프라인'}`).join(' · ');
    node.textContent = '웹 연결 정상' + (status ? ' · ' + status : '');
    node.className = 'success';
    const secondary=bots.find(b=>b.id==='secondary');
    if (!r.discordReady || (secondary && !secondary.ready)) {
      help.textContent = r.webOnly ? '현재 WEB_ONLY=true입니다. Railway에서 false로 바꾸세요.' : (!secondary ? '보조 봇을 쓰려면 Railway에 DISCORD_TOKEN_SECONDARY를 설정하세요.' : '오프라인인 봇의 토큰·Discord 초대·Railway 실행 로그를 확인하세요.');
      help.hidden = false;
    }
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
async function downloadFile(path, filename) {
  const blob = await request(path, 'GET', undefined, true);
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function exportBackup() { return downloadFile('/api/export','song-metadata-backup.json'); }
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
function timeValue(value) {
  const parts=String(value ?? 0).split(':');
  if(parts.length>3 || parts.some(x=>!x.trim() || !Number.isFinite(Number(x))))return 0;
  return Math.max(0,parts.reduce((n,x)=>n*60+Number(x),0));
}
function fmt(value) {
  const n = timeValue(value), m = Math.floor(n / 60), sec = n % 60;
  const s = Number.isInteger(sec) ? String(sec).padStart(2, '0') : sec.toFixed(2).padStart(5, '0');
  return `${m}:${s}`;
}
function bindButton(label, cls, handler) {
  const b = el('button', label, cls); b.type = 'button';
  b.addEventListener('click', () => run(b, () => handler(b)));
  return b;
}
function context() {
  return {botTarget:selectedBot, guildId:selectedGuild, team:selectedTeam, channelId:$('#voice-select').value || null};
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
  uploadTicket++; uploading = false; assetId = ''; editingName = null;
  $('#song-form').reset(); $('#song-name').readOnly = false;
  $('#editor-title').textContent = '새 '+categoryNames[currentCategory]+' 등록';
  $('#rename-help').hidden = true;
  $('#save-song').textContent = '＋ '+categoryNames[currentCategory]+' 저장'; $('#save-song').disabled = false;
  $('#song-file').disabled = state?.fileStorage?.uploadsAllowed===false; $('#upload-name').textContent = '선택된 파일 없음';
  $('#upload-progress').hidden = true;
  const audio = $('#upload-preview'); releaseAudio(audio); audio.hidden = true;
  setSource('youtube');
}
async function editSong(song) {
  resetEditor();
  editingName=song.name;
  $('#song-name').value = song.name; $('#song-name').readOnly = false;
  $('#rename-help').hidden=false;
  $('#rename-help').textContent=currentCategory==='entrance'
    ?'여기서 닉네임도 변경할 수 있어요. 타순 이름·사용자 ID 연결·오디오는 유지됩니다. Discord 서버 닉네임은 바뀌지 않습니다.'
    :'이 종류에 등록한 곡의 이름을 바꿉니다. 다른 종류의 같은 이름은 유지되며, 연결된 경기 사운드는 새 이름을 따라갑니다.';
  $('#song-url').value = song.url || ''; $('#song-start').value = fmt(song.start); $('#song-end').value = fmt(song.end);
  $('#member-id').value = song.memberId || ''; assetId = song.assetId || '';
  setSource(song.source || 'youtube');
  $('#editor-title').textContent = categoryNames[currentCategory]+' 수정'; $('#save-song').textContent = '변경 내용 저장';
  if (assetId) {
    $('#upload-name').textContent = song.fileMissing ? '파일 없음 · 다시 업로드하세요' : (song.filename || '등록된 오디오');
    if (!song.fileMissing) { try { await loadEditorPreview(assetId, uploadTicket); } catch(err) { toast(err.message,true); } }
  }
  $('#song-form').scrollIntoView({behavior:'smooth', block:'center'});
}
async function uploadFile(file, onProgress) {
  if (!file) throw new Error('오디오 파일을 선택하세요.');
  if(state?.fileStorage?.uploadsAllowed===false)throw new Error(state.fileStorage.message);
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
    $('#song-start').value = '0:00'; $('#song-end').value = fmt(Math.min(currentCategory==='entrance'?30:3600, Math.floor(result.duration * 100) / 100));
    if (!$('#song-name').value) $('#song-name').value = file.name.replace(/\.[^.]+$/, '').slice(0,64);
    toast('파일 업로드 완료! 재생 구간을 확인하고 '+categoryNames[currentCategory]+'를 저장하세요.');
  } catch (err) {
    if (ticket === uploadTicket) { assetId=''; $('#upload-name').textContent = '업로드 실패 · 다시 선택하세요'; toast(err.message, true); }
  } finally {
    if (ticket === uploadTicket) { uploading=false; $('#save-song').disabled=false; $('#song-file').disabled=state?.fileStorage?.uploadsAllowed===false; $('#upload-progress').hidden=true; }
  }
}
function drawLive(s) {
  const voice = s.voice;
  $('#sidebar-dot').classList.toggle('online', s.ready);
  $('#sidebar-status').textContent = s.ready ? `${s.botLabel || '선택한 봇'} 연결됨` : `${s.botLabel || '선택한 봇'} 미연결`;
  if ($('#voice-bot-label')) $('#voice-bot-label').textContent = s.botLabel || (selectedBot==='secondary'?'백팀 봇':'청팀 봇');
  const voiceTitle = $('#voice-title'); if (voiceTitle) voiceTitle.textContent = voice?.connected ? `${voice.channelName} 연결됨` : '통화방 연결';
  $('#voice-detail').textContent = voice?.connected ? (voice.serverMuted ? '서버 음소거를 해제해주세요.' : `음성 연결 정상${voice.latencyMs !== null ? ' · ' + voice.latencyMs + 'ms' : ''}`) : '통화방을 선택하고 연결하세요.';
  $('#voice-error').hidden = !voice?.error; $('#voice-error').textContent = voice?.error || '';
  $('#playing-title').textContent = s.nowPlaying?.title || '재생 대기 중';
  $('#playing-status').textContent = s.nowPlaying?.error || statusNames[s.nowPlaying?.status] || 'Discord 통화방에서 재생됩니다';
  if (document.activeElement !== $('#volume')) { $('#volume').value = Math.round(s.state.volume * 100); $('#volume-value').textContent = $('#volume').value + '%'; }
  const off = !s.ready;
  for (const id of ['connect','disconnect','next-player','stop','activate-team']) $('#' + id).disabled = off;
  $('#volume').disabled = off;
}
async function loadLive() {
  // 실시간 음성/현재곡 상태만 조회한다. 이 API는 Firestore를 읽지 않는다.
  if (!csrf || !state) return;
  const params = new URLSearchParams();
  params.set('bot_target', selectedBot);
  if (selectedGuild) params.set('guild_id', selectedGuild);
  const live = await api('/api/live?' + params);
  if (!state || live.botTarget !== selectedBot) return;
  state = {...state, ...live};
  drawLive(state);
}

async function loadState(full = true) {
  if (!csrf) return;
  const ticket = ++stateTicket;
  const params = new URLSearchParams();
  params.set('bot_target', selectedBot);
  if (selectedGuild) params.set('guild_id', selectedGuild);
  if (selectedTeam) params.set('team', selectedTeam);
  const s = await api('/api/state?' + params);
  if (ticket !== stateTicket) return;
  state = s; selectedBot = s.botTarget || selectedBot; selectedGuild = s.guildId || ''; selectedTeam = s.team;
  drawLive(s); drawStorage(s); rememberWorkspace();
  if (!full) return;
  const usableBots=(s.botTargets || []).filter(b=>b.id==='primary' || b.userId || b.ready);
  fillSelect($('#bot-select'), usableBots.map(b=>({id:b.id,name:`${b.name}${b.ready?' · 온라인':' · 오프라인'}`})), selectedBot);
  fillSelect($('#guild-select'), s.guilds, selectedGuild, s.guilds.length ? null : '연결된 서버 없음');
  fillSelect($('#team-select'), s.teams.map(t=>({id:t,name:t})), selectedTeam);
  const chosenVoice = $('#voice-select').value;
  fillSelect($('#voice-select'), s.channels.map(c=>({id:c.id,name:`${c.name} · ${c.members}명${c.occupiedByOtherBot ? ` (${c.occupiedByLabel || '다른 봇'} 사용 중 · 선택 불가)` : (c.available ? '' : ' (권한 부족)')}`})), chosenVoice || s.voice?.channelId || '', '통화방 선택');
  $('#storage-badge').textContent = s.storage === 'firebase' ? '곡 정보: Firebase' : '곡 정보: 로컬 DB';
  const notes = [];
  if (s.webOnly) notes.push('웹 확인 모드입니다. 실제 접속·재생은 WEB_ONLY=false로 바꾸고 봇을 실행하세요.');
  else if (!s.ready) notes.push('봇이 Discord에 연결되지 않았습니다. 토큰·실행 로그를 확인하세요. 곡 등록은 가능합니다.');
  if (s.storage === 'local') notes.push('로컬 저장 모드: 재배포 시 파일을 유지하려면 DATA_DIR에 영구 저장소가 필요합니다.');
  if (s.activeTeam !== selectedTeam) notes.push(`${s.botLabel || '선택한 봇'}의 현재 경기 팀은 “${s.activeTeam}”입니다. 팀을 바꾸려면 “이 봇의 경기 팀으로 설정”을 누르세요.`);
  if (s.otherBotVoice) notes.push(`${s.otherBotVoice.botLabel || '다른 봇'}이 “${s.otherBotVoice.channelName}” 통화방을 사용 중입니다. 선택한 봇은 같은 통화방에 들어갈 수 없습니다.`);
  $('#workspace-note').hidden = !notes.length; $('#workspace-note').textContent = notes.join(' ');
  $('#upload-limit').textContent = `MP3 · WAV · M4A · OGG · 최대 ${s.maxUploadMb}MB`;
  updateCategory(); renderSongs(); renderLineup(); renderEvents(); applyRole();
}
function renderSongs() {
  const list = $('#song-list');
  list.querySelectorAll('audio').forEach(releaseAudio); previewTicket++;
  list.replaceChildren();
  if (!state) return;
  const query = $('#search').value.trim().toLocaleLowerCase();
  $('#song-count').textContent = categorySongs().length;
  const songs = categorySongs().filter(s=>s.name.toLocaleLowerCase().includes(query));
  if (!songs.length) {
    const empty = el('div',null,'empty'); empty.append(el('div','♫','symbol'),el('strong',query ? '검색 결과가 없어요' : '첫 번째 '+categoryNames[currentCategory]+'를 등록해보세요'),el('p',query ? '다른 닉네임 / 곡 이름으로 검색해보세요.' : '등록 폼에서 닉네임 또는 곡 이름과 오디오를 추가하세요.'));
    list.append(empty); return;
  }
  for (const song of songs) {
    const category=song.category || currentCategory;
    const row = el('article', null, 'song-row'), content = el('div');
    row.append(el('div', song.name.slice(0,1), 'song-avatar'));
    content.append(el('h4',song.name,'song-name'));
    const meta = el('div',null,'song-meta');
    meta.append(el('span',song.source === 'upload' ? 'FILE' : 'YOUTUBE','song-tag'),el('span',`${fmt(song.start)} — ${fmt(song.end)} · ${Math.round((timeValue(song.end)-timeValue(song.start))*100)/100}초`));
    if (song.memberId) meta.append(el('span','ID 연결','song-tag'));
    if (song.fileMissing) meta.append(el('span','파일 없음','error-text'));
    content.append(meta);
    const actions = el('div',null,'song-actions');
    actions.append(bindButton('▶ 재생','soft',()=>control('play',{name:song.name,category},categoryNames[category]+' 재생 요청을 처리했습니다.')));
    actions.append(bindButton('통화방 5초','ghost',()=>control('play',{name:song.name,category,preview:true})));
    if (['admin','player'].includes(currentUser?.role)) actions.append(bindButton('수정','ghost',()=>editSong(song)));
    if (song.source === 'upload' && !song.fileMissing) {
      actions.append(bindButton('원본 다운로드','ghost',()=>downloadFile('/api/media/'+encodeURIComponent(song.assetId)+'?download=1',song.filename || 'audio')));
      actions.append(bindButton('내 기기에서 듣기','ghost',async()=>{
        const ticket = ++previewTicket;
        if (previewAudio) releaseAudio(previewAudio);
        let audio = row.querySelector('audio');
        if (!audio) { audio = el('audio'); audio.controls=true; audio.preload='metadata'; row.append(audio); }
        if (!await attachMedia(audio, song.assetId)) return;
        if (ticket !== previewTicket) { releaseAudio(audio); return; }
        audio.currentTime = timeValue(song.start);
        audio.ontimeupdate = ()=>{ if (audio.currentTime >= timeValue(song.end)) audio.pause(); };
        previewAudio=audio; await audio.play();
      }));
    } else if (song.source !== 'upload') {
      actions.append(bindButton('영상 열기','ghost',()=>{
        const u = new URL(song.url);
        if (!['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be'].includes(u.hostname) || !['http:','https:'].includes(u.protocol)) throw new Error('올바른 YouTube 주소가 아닙니다. 링크를 수정하세요.');
        u.protocol='https:'; u.searchParams.set('t',Math.floor(timeValue(song.start))); window.open(u.href,'_blank','noopener,noreferrer');
      }));
    }
    if (currentUser?.role === 'admin') actions.append(bindButton('삭제','ghost delete',async()=>{
      if (!confirm(`“${song.name}” ${categoryNames[category]}를 삭제할까요? 연결된 타순/경기 사운드 설정만 해제됩니다. 다른 종류의 곡과 업로드 원본은 보관됩니다.`)) return;
      await api('/api/songs?'+new URLSearchParams({team:selectedTeam,name:song.name,category}),'DELETE');
      if ($('#song-name').value === song.name) resetEditor();
      await loadState(); toast(categoryNames[category]+'를 삭제했습니다.');
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
function eventTracks(event) {
  const tracks=event.custom?.tracks;
  if(Array.isArray(tracks)) return tracks;
  if(event.custom?.songName) return [{type:'song',songName:event.custom.songName}];
  if(event.custom?.assetId) return [{type:'asset',assetId:event.custom.assetId,filename:event.custom.filename || '업로드 오디오'}];
  return [];
}
function eventModeName(mode){return {single:'단곡',random:'랜덤',sequence:'여러곡 순차'}[mode] || '단곡';}
async function saveEventTracks(event, tracks, playMode) {
  await api('/api/events','PUT',{team:selectedTeam,key:event.key,tracks,playMode});
  await loadState();
}
function renderEvents() {
  const grid=$('#events-grid'); grid.replaceChildren();
  for (const event of state.events) {
    const card=el('article',null,'card event-card');
    const titleRow=el('div',null,'section-head event-title-row');
    const titleWrap=el('div');
    titleWrap.append(el('h3',event.label));
    if(event.customEvent) titleWrap.append(el('span','직접 추가','badge'));
    titleRow.append(titleWrap);
    if(currentUser?.role==='admin' && event.customEvent){
      const actions=el('div',null,'event-mini-actions');
      actions.append(bindButton('이름 수정','ghost',async()=>{
        const next=prompt('새 상황 이름을 입력하세요.',event.label); if(next===null)return;
        const label=next.trim();if(!label)throw new Error('상황 이름을 입력하세요.');
        await api('/api/events','PUT',{team:selectedTeam,key:event.key,label}); await loadState();toast('경기 상황 이름을 수정했습니다.');
      }));
      actions.append(bindButton('상황 삭제','ghost delete',async()=>{
        if(!confirm(`“${event.label}” 상황을 삭제할까요? 연결된 사운드 목록도 함께 삭제됩니다.`))return;
        await api(`/api/events?team=${encodeURIComponent(selectedTeam)}&key=${encodeURIComponent(event.key)}`,'DELETE'); await loadState();toast('경기 상황을 삭제했습니다.');
      }));
      titleRow.append(actions);
    }
    card.append(titleRow);

    const tracks=eventTracks(event), mode=event.custom?.playMode || 'single';
    const summary=tracks.length ? `${eventModeName(mode)} · ${tracks.length}곡 연결됨` : (event.customEvent ? '아직 연결된 사운드가 없습니다.' : `기본 효과음 ${event.bundledCount}개 · 무작위 재생`);
    card.append(el('p',summary,'tiny event-summary'));
    card.append(bindButton('▶ 상황 재생','soft',()=>control('event',{key:event.key})));

    if (currentUser?.role !== 'admin') { grid.append(card); continue; }

    const settings=el('div',null,'event-settings');
    const modeLabel=el('label','재생 방식'), modeSelect=el('select');
    [['single','단곡'],['random','랜덤'],['sequence','여러곡 순차']].forEach(([id,name])=>{const o=el('option',name);o.value=id;if(id===mode)o.selected=true;modeSelect.append(o);});
    modeSelect.addEventListener('change',()=>run(modeSelect,async()=>{
      await saveEventTracks(event,tracks,modeSelect.value); toast(`재생 방식을 ${eventModeName(modeSelect.value)}(으)로 변경했습니다.`);
    }));
    modeLabel.append(modeSelect); settings.append(modeLabel);
    const help={single:'첫 번째 곡 1개를 항상 재생합니다.',random:'연결한 곡 중 하나를 매번 무작위로 재생합니다.',sequence:'상황 버튼을 누를 때마다 다음 곡을 순서대로 재생합니다.'};
    settings.append(el('p',help[mode] || help.single,'tiny event-mode-help'));

    const list=el('div',null,'event-track-list');
    if(!tracks.length){list.append(el('p','연결된 곡이 없습니다. 아래에서 상황별 노래나 파일을 추가하세요.','tiny'));}
    tracks.forEach((track,index)=>{
      const row=el('div',null,'event-track-row');
      row.append(el('span',`${index+1}. ${track.type==='song' ? track.songName : (track.filename || '업로드 오디오')}`));
      row.append(bindButton('빼기','ghost delete',()=>run(null,async()=>{
        const next=tracks.filter((_,i)=>i!==index); await saveEventTracks(event,next,mode); toast('목록에서 제거했습니다.');
      })));
      list.append(row);
    });
    settings.append(list);

    const songLabel=el('label','상황별 노래 추가'), chooser=el('select');
    chooser.setAttribute('aria-label',event.label+'에 추가할 상황별 노래');
    fillSelect(chooser,categorySongs('situation').map(s=>({id:s.name,name:s.name})),null,'곡을 선택하세요');
    songLabel.append(chooser); settings.append(songLabel);
    settings.append(bindButton('+ 선택한 노래 추가','ghost',async()=>{
      if(!chooser.value)throw new Error('상황별 노래를 먼저 선택하세요.');
      const next=[...tracks,{type:'song',songName:chooser.value}];
      await saveEventTracks(event,next,mode); toast('경기 상황 곡 목록에 추가했습니다.');
    }));

    const fileLabel=el('label','오디오 파일 추가'), input=el('input'); input.type='file'; input.disabled=state?.fileStorage?.uploadsAllowed===false; input.accept='.mp3,.wav,.ogg,.m4a,.flac,.aac,.opus,.webm';
    input.addEventListener('change',()=>run(null,async()=>{
      const file=input.files[0]; if(!file)return; input.disabled=true;
      try { toast('경기 사운드를 업로드하고 있습니다.'); const result=await uploadFile(file); const next=[...tracks,{type:'asset',assetId:result.id,filename:result.name || file.name}]; await saveEventTracks(event,next,mode); toast('오디오를 경기 상황 목록에 추가했습니다.'); }
      finally {input.disabled=state?.fileStorage?.uploadsAllowed===false;}
    }));
    fileLabel.append(input); settings.append(fileLabel);

    if(tracks.length) settings.append(bindButton(event.customEvent?'목록 모두 비우기':'기본 효과음으로 복원','ghost delete',async()=>{
      const msg=event.customEvent?'연결한 모든 곡을 목록에서 비울까요?':'등록한 곡 목록을 지우고 기본 효과음으로 돌아갈까요?';
      if(!confirm(msg))return; await saveEventTracks(event,[],mode); toast(event.customEvent?'곡 목록을 비웠습니다.':'기본 효과음으로 복원했습니다.');
    }));
    card.append(settings); grid.append(card);
  }
}
async function diagnostics() {
  const d=await api('/api/diagnostics');
  const values=$('#diagnostic-values'); values.replaceChildren();
  const rows=[['오디오 저장',d.fileStorage?.label || '확인 필요'],['파일 저장 경로',d.fileStorage?.uploadDir || ''],['Discord 연결',d.discordReady?'연결됨':'미연결'],['실행 모드',d.webOnly?'웹 확인 모드':'봇 + 웹'],['저장소',d.storage==='firebase'?'Firebase':'로컬 SQLite'],['FFmpeg',d.ffmpeg?'설치됨':'미설치'],['ffprobe',d.ffprobe?'설치됨':'미설치'],['Deno',d.deno?'설치됨':'미설치'],...Object.entries(d.packages),['YouTube 쿠키',d.cookiesConfigured?'설정됨':'미설정'],['웹 인증',d.authMode==='bearer'?'로그인 토큰 (교차 사이트 쿠키 불필요)':'같은 사이트 쿠키'],['Railway API',apiBase || location.origin],['관리 웹',d.webUrl || location.origin],['허용된 웹 주소',(d.webOrigins || []).join(', ') || '같은 주소에서만 허용'],['업로드 제한',`${d.maxUploadMb}MB/파일 · 총 ${d.maxStorageMb}MB`]];
  for (const [k,v] of rows) { const row=el('div',null,'diagnostic-row'), strong=el('strong',v); if(['미설치','미연결'].includes(v))strong.classList.add('warn');row.append(el('span',k),strong);values.append(row); }
  const checks=$('#diagnostic-checks'); checks.replaceChildren();
  for(const text of d.checks){const row=el('div',null,'check-item');row.append(el('span','✓'),el('p',text));checks.append(row);}
}
function applyRole() {
  const admin = currentUser?.role === 'admin';
  $('#signed-user').textContent = currentUser ? `${currentUser.displayName} · ${admin ? '관리자' : '재생자'}` : '';
  $$('.admin-only').forEach(x=>x.hidden=!admin);
  // 재생자도 등장곡 등록/오디오 업로드/수정 가능. 운영 설정은 관리자만 보인다.
  const editor = $('.song-editor'); if (editor) editor.hidden = !['admin','player'].includes(currentUser?.role);
  for (const sel of ['#new-team','#activate-team','#save-lineup','#export-backup','#export-audio-backup']) { const x=$(sel); if(x)x.hidden=!admin; }
  $$('#lineup-grid select').forEach(x=>x.disabled=!admin);
}
async function loadUsers() {
  if (currentUser?.role !== 'admin') return;
  const r=await api('/api/users'); const list=$('#user-list'); list.replaceChildren();
  for(const u of r.users){
    const row=el('article',null,'user-row'), info=el('div');
    info.append(el('strong',u.displayName || u.username),el('p',`@${u.username} · ${u.role==='admin'?'관리자':u.role==='player'?'재생자':'승인 대기'}${u.enabled===false?' · 사용 중지':''}`,'tiny')); row.append(info);
    const actions=el('div',null,'user-actions');
    if(u.username==='admin'){actions.append(el('span','기본 관리자','badge'));}
    else {
      if(u.role!=='player') actions.append(bindButton('재생자로 승인','primary',async()=>{await api('/api/users/'+encodeURIComponent(u.username),'PUT',{role:'player',enabled:true});await loadUsers();toast('재생자로 승인했습니다.');}));
      else actions.append(bindButton('승인 대기로 변경','ghost',async()=>{await api('/api/users/'+encodeURIComponent(u.username),'PUT',{role:'pending',enabled:true});await loadUsers();}));
      actions.append(bindButton(u.enabled===false?'사용 재개':'사용 중지','ghost',async()=>{await api('/api/users/'+encodeURIComponent(u.username),'PUT',{role:u.role,enabled:u.enabled===false});await loadUsers();}));
      actions.append(bindButton('삭제','ghost delete',async()=>{if(!confirm(`${u.displayName||u.username} 계정을 삭제할까요?`))return;await api('/api/users/'+encodeURIComponent(u.username),'DELETE');await loadUsers();toast('계정을 삭제했습니다.');}));
    }
    row.append(actions); list.append(row);
  }
}
function setTab(name) {
  $$('.nav').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  $$('.tab-panel').forEach(p=>{p.hidden=p.id!=='tab-'+name;});
  $('#page-title').textContent = {songs:'음악 라이브러리',lineup:'타순 관리',events:'경기 사운드',diagnostics:'연결 진단',users:'재생자 관리'}[name];
  if(name==='diagnostics')run(null,diagnostics); if(name==='users')run(null,loadUsers);
}
$('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();const b=e.submitter;b.disabled=true;$('#login-error').textContent='';
  try{const r=await api('/api/login','POST',{username:$('#username').value.trim().toLowerCase(),password:$('#password').value,authMode:'bearer'});if(!r.accessToken)throw new Error('Railway 서버 코드를 새 버전으로 교체하세요.');rememberToken(r.accessToken);csrf=r.csrf;currentUser=r.user;restoreWorkspace();$('#password').value='';$('#login-screen').hidden=true;$('#app').hidden=false;applyRole();await loadState();applyRole();}
  catch(err){$('#login-error').textContent=err.message;}finally{b.disabled=false;}
});
$('#show-signup').addEventListener('click',()=>{$('#login-form').hidden=true;$('#signup-form').hidden=false;});
$('#hide-signup').addEventListener('click',()=>{$('#signup-form').hidden=true;$('#login-form').hidden=false;});
$('#signup-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;$('#signup-error').textContent='';try{const r=await api('/api/signup','POST',{username:$('#signup-username').value.trim().toLowerCase(),displayName:$('#signup-display').value.trim(),password:$('#signup-password').value});toast(r.message);$('#signup-form').reset();$('#signup-form').hidden=true;$('#login-form').hidden=false;}catch(err){$('#signup-error').textContent=err.message;}finally{b.disabled=false;}});
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
    if(!selectedTeam)throw new Error('팀을 먼저 선택하세요.');
    const name=$('#song-name').value.trim(), category=currentCategory, team=selectedTeam, ticket=uploadTicket;
    if(editingName===null && categorySongs().some(s=>s.name===name) && !confirm('같은 종류에 같은 이름의 곡이 있습니다. 덮어쓸까요?'))return;
    const result=await api('/api/songs','POST',{team,name,category,oldName:editingName,
      url:$('#song-url').value,source,assetId,start:$('#song-start').value,end:$('#song-end').value,memberId:$('#member-id').value});
    if(ticket===uploadTicket)resetEditor();
    await loadState();toast(result.warning || categoryNames[category]+'를 저장했습니다. 닉네임 변경도 반영됐어요.',!!result.warning);
  });
});
$$('[data-category]').forEach(button=>button.addEventListener('click',()=>setCategory(button.dataset.category)));
$('#bot-select').addEventListener('change',()=>run(null,async()=>{selectedBot=$('#bot-select').value||'primary';selectedGuild='';selectedTeam='';resetEditor();await loadState();}));
$('#guild-select').addEventListener('change',()=>run(null,async()=>{selectedGuild=$('#guild-select').value;selectedTeam='';resetEditor();await loadState();}));
$('#team-select').addEventListener('change',()=>run(null,async()=>{selectedTeam=$('#team-select').value;resetEditor();await loadState();}));
$('#new-team').addEventListener('click',()=>{$('#team-name').value='';$('#team-dialog').showModal();$('#team-name').focus();});
$('#cancel-team').addEventListener('click',()=>$('#team-dialog').close());
$('#team-form').addEventListener('submit',e=>{e.preventDefault();run(e.submitter,async()=>{const team=$('#team-name').value.trim();await api('/api/team','POST',{team});selectedTeam=team;resetEditor();$('#team-dialog').close();await loadState();toast('새 팀을 추가했습니다.');});});
$('#activate-team').addEventListener('click',()=>run($('#activate-team'),async()=>{await api('/api/team','POST',{team:selectedTeam,guildId:selectedGuild,botTarget:selectedBot,activate:true});await loadState();toast((state?.botLabel || (selectedBot==='secondary'?'백팀 봇':'청팀 봇'))+'의 경기 팀을 변경했습니다.');}));
$('#connect').addEventListener('click',()=>run($('#connect'),()=>control('connect',{},'통화방에 연결했습니다.')));
$('#disconnect').addEventListener('click',()=>run($('#disconnect'),()=>control('disconnect',{},'통화방에서 퇴장했습니다.')));
$('#stop').addEventListener('click',()=>run($('#stop'),()=>control('stop',{},'재생을 정지했습니다.')));
$('#next-player').addEventListener('click',()=>run($('#next-player'),()=>control('next')));
$('#volume').addEventListener('input',()=>{$('#volume-value').textContent=$('#volume').value+'%';});
$('#volume').addEventListener('change',()=>run(null,()=>control('volume',{value:Number($('#volume').value)})));
$('#save-lineup').addEventListener('click',()=>run($('#save-lineup'),async()=>{const lineup={};$$('#lineup-grid select').forEach(s=>{lineup[s.dataset.order]=s.value;});await api('/api/lineup','PUT',{team:selectedTeam,lineup});await loadState();toast('타순을 저장했습니다.');}));
$('#event-create-form').addEventListener('submit',e=>{e.preventDefault();run(e.submitter,async()=>{
  if(currentUser?.role!=='admin')throw new Error('관리자만 경기 상황을 추가할 수 있습니다.');
  if(!selectedTeam)throw new Error('팀을 먼저 선택하세요.');
  const label=$('#event-name').value.trim();if(!label)throw new Error('상황 이름을 입력하세요.');
  const playMode=$('#event-create-mode').value;
  await api('/api/events','POST',{team:selectedTeam,label,playMode});
  $('#event-name').value='';await loadState();toast('새 경기 상황을 추가했습니다.');
});});
$('#check-connection').addEventListener('click',()=>run($('#check-connection'),checkConnection));
$('#export-backup').addEventListener('click',()=>run($('#export-backup'),exportBackup));
$('#export-audio-backup').addEventListener('click',()=>run($('#export-audio-backup'),async()=>{toast('음악 백업을 준비합니다. 파일이 많으면 시간이 걸릴 수 있어요.');await downloadFile('/api/backup','appearance-music-backup.zip');toast('백업 다운로드를 시작했습니다.');}));
window.addEventListener('pagehide',clearMedia);
$('#reload-users').addEventListener('click',()=>run($('#reload-users'),loadUsers));
$('#reload-diagnostics').addEventListener('click',()=>run($('#reload-diagnostics'),diagnostics));
setInterval(()=>{if(csrf && state && !document.hidden)loadLive().catch(err=>{if(csrf)toast(err.message,true);});},10000);
configureClient();
(async()=>{
  const connected=await checkConnection();
  if(!connected || !accessToken)return;
  try{const r=await api('/api/session');csrf=r.csrf;currentUser=r.user;restoreWorkspace();$('#login-screen').hidden=true;$('#app').hidden=false;applyRole();await loadState();applyRole();}
  catch(err){if(csrf)toast(err.message,true);else showLogin();}
})();
