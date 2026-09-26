'use strict';
// Firebase predeploy check. No packages, credentials or network access required.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
function check(rootPath = root) {
  const manifest = JSON.parse(fs.readFileSync(path.join(rootPath, 'firebase.json'), 'utf8'));
  if (manifest.hosting?.public !== 'static') throw new Error('hosting.public must be static, never the project root.');
  const sandbox = {window: {}};
  vm.runInNewContext(fs.readFileSync(path.join(rootPath, 'static', 'config.js'), 'utf8'), sandbox, {timeout:100});
  const raw = String(sandbox.window.APPEARANCE_CONFIG?.API_BASE_URL || '').trim();
  let api = '';
  if (raw) {
    const u = new URL(raw);
    if (u.protocol !== 'https:' || u.username || u.password || u.pathname !== '/' || u.search || u.hash || ['localhost','127.0.0.1','[::1]'].includes(u.hostname) || u.hostname.endsWith('.railway.internal')) throw new Error('Use a public HTTPS Railway URL without a path.');
    api = u.origin;
  }
  const policies = manifest.hosting.headers.flatMap(x=>x.headers).filter(x=>x.key.toLowerCase()==='content-security-policy');
  const connectTokens = policies.flatMap(h=>h.value.split(';').map(rule=>rule.trim().split(/\s+/)).filter(t=>t[0]==='connect-src').flatMap(t=>t.slice(1)));
  if (api && !connectTokens.includes(api) && !connectTokens.includes('https://*.up.railway.app')) throw new Error('CSP does not allow the Railway URL. Re-run: python setup_deploy.py');
  if (!api && !connectTokens.includes('https://*.up.railway.app')) throw new Error('Blank API mode requires connect-src https://*.up.railway.app.');
  for (const name of ['index.html','style.css','app.js','config.js','manifest.webmanifest','sw.js','pwa.js','download.html','offline.html']) if (!fs.existsSync(path.join(rootPath,'static',name))) throw new Error('Missing static file: '+name);
  const unexpected = fs.readdirSync(path.join(rootPath,'static')).filter(n=>!['index.html','style.css','app.js','config.js','404.html','manifest.webmanifest','sw.js','pwa.js','download.html','offline.html','icons'].includes(n));
  const appManifest=JSON.parse(fs.readFileSync(path.join(rootPath,'static','manifest.webmanifest'),'utf8'));
  if(appManifest.scope!=='/' || appManifest.start_url!=='/')throw new Error('PWA scope/start URL must share the website root.');
  for(const icon of appManifest.icons){if(!/^\/icons\/[a-z0-9-]+\.png$/.test(icon.src) || !fs.existsSync(path.join(rootPath,'static',icon.src.slice(1))))throw new Error('Missing or unsafe PWA icon');}
  if (unexpected.length) throw new Error('Unexpected files in public static folder; review before publishing: '+unexpected.join(', '));
  if (!api && manifest.hosting.site !== 'appearance-song') throw new Error('Blank API mode is prepared only for https://appearance-song.web.app.');
  if (!manifest.hosting.site) throw new Error('Firebase Hosting site is not selected.');
  return {api: api || '(set on login page)', site: manifest.hosting.site};
}
if (require.main === module) {
  try { const r=check(); console.log(`Hosting check passed: site=${r.site}, API=${r.api}`); }
  catch (e) { console.error('Hosting check failed: '+e.message); process.exitCode=1; }
}
module.exports={check};
