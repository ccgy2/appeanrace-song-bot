'use strict';
// No network or credentials: execute the shipped scheduler and request guards
// with deterministic timers and deferred responses.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
function sourceBetween(start, end) {
  const a = app.indexOf(start), b = app.indexOf(end, a);
  assert(a >= 0 && b > a, 'shipped function exists');
  return app.slice(a, b);
}
const factory = vm.runInNewContext(sourceBetween('function createLivePoller(', '\nconst livePoller =') + '\ncreateLivePoller;');
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
function deferred() { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j; }); return {promise, resolve, reject}; }
function fixture(load = async () => {}) {
  let nextId = 0;
  const timers = new Map(), errors = [];
  const ctx = {enabled:true, interval:60000, calls:0};
  ctx.poller = factory({
    load: () => { ctx.calls++; return load(); },
    enabled: () => ctx.enabled,
    interval: () => ctx.interval,
    onError: e => errors.push(e.message),
    setTimer: (fn, delay) => { const id = ++nextId; timers.set(id, {fn, delay}); return id; },
    clearTimer: id => timers.delete(id),
  });
  ctx.timers = timers; ctx.errors = errors;
  ctx.delay = () => { assert.equal(timers.size, 1); return [...timers.values()][0].delay; };
  ctx.fire = async () => { assert.equal(timers.size, 1); const [id, t] = [...timers][0]; timers.delete(id); t.fn(); await flush(); };
  return ctx;
}

test('idle, connected and playback cadences use one timer; waking replaces it', async () => {
  const f = fixture(); f.poller.wake(); assert.equal(f.delay(), 60000);
  await f.fire(); assert.equal(f.calls, 1); assert.equal(f.delay(), 60000);
  f.interval = 30000; f.poller.wake(); assert.equal(f.delay(), 30000);
  f.interval = 10000; f.poller.wake(); assert.equal(f.delay(), 10000);
  f.poller.wake({immediate:true}); f.poller.wake({immediate:true}); assert.equal(f.delay(), 0);
  await f.fire(); assert.equal(f.calls, 2); assert.equal(f.delay(), 10000);
});

test('slow responses never overlap; repeated wake requests collapse to one refresh', async () => {
  const pending = deferred(); const f = fixture(() => pending.promise);
  f.poller.wake({immediate:true}); await f.fire();
  assert.equal(f.calls, 1); assert.equal(f.timers.size, 0);
  f.poller.wake({immediate:true}); f.poller.wake({immediate:true});
  assert.equal(f.calls, 1); assert.equal(f.timers.size, 0);
  pending.resolve(); await flush(); assert.equal(f.delay(), 0);
  await f.fire(); assert.equal(f.calls, 2); assert.equal(f.delay(), 60000);
});

test('hidden/offline/logout stop scheduled and in-flight follow-up requests', async () => {
  const pending = deferred(); const f = fixture(() => pending.promise);
  f.poller.wake(); f.enabled = false; f.poller.stop();
  assert.equal(f.timers.size, 0); f.poller.wake({immediate:true}); assert.equal(f.timers.size, 0);
  f.enabled = true; f.poller.wake({immediate:true}); await f.fire();
  f.enabled = false; f.poller.stop(); pending.resolve(); await flush();
  assert.equal(f.calls, 1); assert.equal(f.timers.size, 0);
  f.enabled = true; f.poller.wake({immediate:true}); assert.equal(f.delay(), 0);
});

test('outages back off to five minutes, notify once and recover automatically', async () => {
  let failing = true;
  const f = fixture(async () => { if (failing) throw new Error('offline'); });
  f.interval = 10000; f.poller.wake();
  for (const delay of [15000, 30000, 60000, 120000, 240000, 300000, 300000]) {
    await f.fire(); assert.equal(f.delay(), delay);
  }
  assert.deepEqual(f.errors, ['offline']);
  failing = false; await f.fire(); assert.equal(f.delay(), 10000);
  failing = true; await f.fire(); assert.equal(f.delay(), 15000);
  assert.deepEqual(f.errors, ['offline', 'offline']);
  f.poller.wake({immediate:true, resetFailures:true}); assert.equal(f.delay(), 0);
});

function liveFixture() {
  const pending = deferred(); let draws = 0;
  const ctx = vm.createContext({csrf:'csrf', accessToken:'token', selectedBot:'primary', selectedGuild:'10', selectedTeam:'team', stateTicket:4,
    state:{marker:'current', state:{volume:0.5}}, URLSearchParams, api:() => pending.promise, drawLive:() => draws++});
  vm.runInContext(sourceBetween('async function loadLive()', '\nasync function loadState') + '\nglobalThis.load = loadLive;', ctx);
  return {ctx, pending, draws:() => draws};
}

test('live updates never apply an old guild, bot, team, command or login response', async () => {
  for (const [key, value] of [['selectedGuild','11'], ['selectedBot','secondary'], ['selectedTeam','other'], ['stateTicket',5], ['accessToken','replacement'], ['csrf','']]) {
    const f = liveFixture(); const promise = f.ctx.load(); f.ctx[key] = value;
    f.pending.resolve({botTarget:'primary', guildId:'10', nowPlaying:{status:'playing'}}); await promise;
    assert.equal(f.draws(), 0, key); assert.equal(f.ctx.state.marker, 'current');
  }
  const f = liveFixture(); const promise = f.ctx.load();
  f.pending.resolve({botTarget:'primary', guildId:'11', nowPlaying:{status:'playing'}}); await promise;
  assert.equal(f.draws(), 0, 'unexpected server guild is rejected');
});

test('fresh live updates draw; obsolete failures are quiet while current failures propagate', async () => {
  const fresh = liveFixture(); const promise = fresh.ctx.load();
  fresh.pending.resolve({botTarget:'primary', guildId:'10', nowPlaying:{status:'playing'}}); await promise;
  assert.equal(fresh.draws(), 1); assert.equal(fresh.ctx.state.nowPlaying.status, 'playing');
  const stale = liveFixture(); const old = stale.ctx.load(); stale.ctx.stateTicket++;
  stale.pending.reject(new Error('old timeout')); await old;
  const current = liveFixture(); const active = current.ctx.load(); current.pending.reject(new Error('current timeout'));
  await assert.rejects(active, /current timeout/);
});

test('playback commands use their response without full library reload; order/gain still reload', async () => {
  for (const action of ['play','connect','disconnect','stop','event','order','next','volume']) {
    const requests = [], loads = [], wake = [], draws = [];
    const ctx = vm.createContext({csrf:'csrf', accessToken:'token', selectedBot:'primary', selectedGuild:'10', selectedTeam:'team', stateTicket:4,
      state:{state:{volume:0.5}}, canPlay:() => true, context:() => ({guildId:'10'}),
      api:async (p, m, data) => { requests.push({p,m,data}); return {voice:{connected:true}, nowPlaying:{status:'playing'}}; },
      loadState:async full => loads.push(full), drawLive:s => draws.push(s), livePoller:{wake:() => wake.push(1)}, toast:() => {}});
    vm.runInContext(sourceBetween('async function control(', '\nfunction setSource') + '\nglobalThis.control = control;', ctx);
    await ctx.control(action);
    assert.equal(requests.length, 1); assert.equal(requests[0].p, '/api/control');
    if (['order','next','volume'].includes(action)) assert.deepEqual(loads, [false]);
    else { assert.deepEqual(loads, []); assert.equal(draws.length, 1); assert.equal(wake.length, 1); }
  }
});


test('an old 401 cannot log out a replacement login session', async () => {
  for (const replaceSession of [false, true]) {
    const pending = deferred(); let loggedOut = 0;
    const ctx = vm.createContext({accessToken:'old', csrf:'csrf', AbortController,
      apiUrl: p => p, fetch: () => pending.promise, setTimeout: () => 1, clearTimeout: () => {},
      showLogin: () => loggedOut++});
    vm.runInContext(sourceBetween('async function request(', '\nasync function api(') + '\nglobalThis.request = request;', ctx);
    const call = ctx.request('/api/live?bot_target=primary');
    if (replaceSession) { ctx.accessToken = 'new'; ctx.csrf = 'new-csrf'; }
    pending.resolve({ok:false, status:401, json:async () => ({error:'expired'})});
    await assert.rejects(call, /expired/);
    assert.equal(loggedOut, replaceSession ? 0 : 1);
  }
});

test('same-origin and Firebase/PWA copies ship the identical optimized code', () => {
  assert.equal(app, fs.readFileSync(path.join(root, '웹사이트/static/app.js'), 'utf8'));
  assert.equal(fs.readFileSync(path.join(root, 'static/sw.js'), 'utf8'), fs.readFileSync(path.join(root, '웹사이트/static/sw.js'), 'utf8'));
  assert(!app.includes('setInterval('), 'no permanent periodic timer remains');
});

const blobFactory = vm.runInNewContext(sourceBetween('function createPrivateBlobCache(', '\nfunction mediaScope()') + '\ncreatePrivateBlobCache;');
test('private media cache reuses same-session blobs and coalesces duplicate in-flight downloads', async () => {
  const pending = deferred(); let calls = 0;
  const cache = blobFactory({scope:() => 'session', load:() => { calls++; return pending.promise; }});
  const first = cache.get('audio'), second = cache.get('audio');
  assert.equal(calls, 1); const blob = {size:1024}; pending.resolve(blob);
  assert.strictEqual(await first, blob); assert.strictEqual(await second, blob);
  assert.strictEqual(await cache.get('audio'), blob); assert.equal(calls, 1);
});

test('private cache evicts least recently used entries without touching in-use blobs', async () => {
  const calls = [], blobs = {a:{size:4}, b:{size:4}, c:{size:4}};
  const cache = blobFactory({scope:() => 'session', maxEntries:2, maxBytes:10, load:async id => { calls.push(id); return blobs[id]; }});
  const stillPlaying = await cache.get('a'); await cache.get('b'); await cache.get('a'); await cache.get('c');
  assert.strictEqual(stillPlaying, blobs.a); assert.strictEqual(await cache.get('a'), stillPlaying);
  await cache.get('b'); assert.deepEqual(calls, ['a','b','c','b']);
  const sizes = {x:6, y:6}; let count = 0;
  const byteBound = blobFactory({scope:() => 'session', maxEntries:2, maxBytes:10, load:async id => { count++; return {size:sizes[id]}; }});
  await byteBound.get('x'); await byteBound.get('y'); await byteBound.get('x'); assert.equal(count, 3, 'byte budget also evicts');
});

test('oversized media is returned for the requested playback but is never retained', async () => {
  let calls = 0; const blob = {size:32 * 1024 * 1024 + 1};
  const cache = blobFactory({scope:() => 'session', load:async () => { calls++; return blob; }});
  assert.strictEqual(await cache.get('large'), blob); assert.strictEqual(await cache.get('large'), blob); assert.equal(calls, 2);
});

test('logout/API/session changes clear private data and reject old in-flight responses', async () => {
  for (const mode of ['logout', 'new-session', 'new-api']) {
    let scope = 'api:A/token:A', calls = 0; const pending = deferred();
    const cache = blobFactory({scope:() => scope, load:() => { calls++; return calls === 1 ? pending.promise : Promise.resolve({size:1, who:'new'}); }});
    const old = cache.get('audio');
    if (mode === 'logout') { cache.clear(); scope = ''; await assert.rejects(cache.get('audio'), /로그인/); scope = 'api:A/token:B'; }
    else scope = mode === 'new-api' ? 'api:B/token:A' : 'api:A/token:B';
    const fresh = await cache.get('audio'); assert.equal(fresh.who, 'new');
    pending.resolve({size:1, who:'old'}); await assert.rejects(old, /로그인 정보가 변경/);
    assert.strictEqual(await cache.get('audio'), fresh); assert.equal(calls, 2, mode);
    cache.clear(); await cache.get('audio'); assert.equal(calls, 3, 'explicit cleanup drops even a same-scope cache');
  }
});
