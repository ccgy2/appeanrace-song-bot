/* Public offline page only. Never cache credentials, API responses, uploads or commands. */
'use strict';
const CACHE_NAME = 'appearance-public-studio-20260926-1';
const PUBLIC_OFFLINE = ['/offline.html','/style.css','/icons/icon-192.png','/icons/icon-512.png','/icons/apple-touch-icon.png'];
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE_NAME).then(cache=>cache.addAll(PUBLIC_OFFLINE)));
  // A later version waits until the user accepts, avoiding discarded editor drafts.
});
self.addEventListener('activate',event=>{
  event.waitUntil((async()=>{
    for(const key of await caches.keys())if(key.startsWith('appearance-public-') && key!==CACHE_NAME)await caches.delete(key);
    await self.clients.claim();
  })());
});
self.addEventListener('message',event=>{if(event.data?.type==='SKIP_WAITING')self.skipWaiting();});
self.addEventListener('fetch',event=>{
  const req=event.request,url=new URL(req.url);
  if(req.method!=='GET' || url.origin!==self.location.origin || url.pathname.startsWith('/api/') || req.headers.has('Authorization'))return;
  if(req.mode==='navigate'){
    event.respondWith(fetch(req).catch(async()=>{
      const fallback=await caches.match('/offline.html',{cacheName:CACHE_NAME});
      return fallback || new Response('인터넷 연결이 필요합니다. 연결 후 새로고침하세요.',{status:503,headers:{'Content-Type':'text/plain; charset=utf-8'}});
    }));return;
  }
  if(PUBLIC_OFFLINE.includes(url.pathname))event.respondWith(fetch(req).catch(async()=>{
    return (await caches.match(url.pathname,{cacheName:CACHE_NAME})) || Response.error();
  }));
});
