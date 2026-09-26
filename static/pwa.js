'use strict';
(()=>{
  let installPrompt=null,registration=null;
  const $=selector=>document.querySelector(selector);
  const standalone=()=>window.matchMedia('(display-mode: standalone)').matches || navigator.standalone===true;
  const ios=/iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform==='MacIntel' && navigator.maxTouchPoints>1);
  function draw(){
    document.documentElement.classList.toggle('is-installed',standalone());
    const button=$('#pwa-install-button'),status=$('#pwa-install-status');
    if(button){button.hidden=standalone() || !installPrompt;button.disabled=!installPrompt;}
    if(status)status.textContent=standalone()?'앱 모드로 실행 중입니다. 같은 계정으로 컨트롤 화면을 이용하세요.':
      installPrompt?'설치 준비가 됐습니다. 아래 설치 버튼을 눌러 브라우저 안내를 확인하세요.':
      ios?'iPhone · iPad: Safari의 공유 메뉴에서 ‘홈 화면에 추가’를 선택하세요.':
      '설치 버튼이 보이지 않으면 Chrome 메뉴의 ‘앱 설치’ 또는 ‘홈 화면에 추가’를 이용하세요. 메뉴 이름은 브라우저마다 다를 수 있습니다.';
  }
  window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;draw();});
  window.addEventListener('appinstalled',()=>{installPrompt=null;draw();const status=$('#pwa-install-status');if(status)status.textContent='설치를 완료했습니다. 홈 화면의 APPEARANCE 아이콘으로 실행하세요.';});
  window.matchMedia('(display-mode: standalone)').addEventListener?.('change',draw);
  $('#pwa-install-button')?.addEventListener('click',async()=>{
    if(!installPrompt)return;const prompt=installPrompt;installPrompt=null;
    try{await prompt.prompt();const choice=await prompt.userChoice;draw();if(choice.outcome==='dismissed')$('#pwa-install-status').textContent='설치를 취소했습니다. 브라우저 메뉴에서 다시 설치할 수 있습니다.';}
    catch{draw();$('#pwa-install-status').textContent='브라우저 메뉴에서 설치하거나 홈 화면에 추가하세요.';}
  });
  function offerUpdate(worker){
    if(!worker || $('#pwa-update-banner'))return;
    const banner=document.createElement('div');banner.id='pwa-update-banner';banner.className='pwa-update-banner';banner.setAttribute('role','status');
    const text=document.createElement('span');text.textContent='새 앱 버전이 준비됐어요. 수정 중인 내용을 저장한 뒤 업데이트하세요.';
    const button=document.createElement('button');button.type='button';button.className='primary';button.textContent='업데이트';
    button.addEventListener('click',()=>{
      if(!confirm('다른 탭을 포함해 수정 중인 내용을 모두 저장했나요? 현재 화면을 새로고침합니다.'))return;
      navigator.serviceWorker.addEventListener('controllerchange',()=>location.reload(),{once:true});
      worker.postMessage({type:'SKIP_WAITING'});button.disabled=true;button.textContent='적용 중…';
    });banner.append(text,button);document.body.append(banner);
  }
  draw();
  if('serviceWorker' in navigator && window.isSecureContext){
    navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'}).then(reg=>{
      registration=reg;if(reg.waiting)offerUpdate(reg.waiting);
      reg.addEventListener('updatefound',()=>{const worker=reg.installing;worker?.addEventListener('statechange',()=>{if(worker.state==='installed' && navigator.serviceWorker.controller)offerUpdate(reg.waiting);});});
    }).catch(()=>{const node=$('#pwa-install-status');if(node)node.textContent='앱 설치 준비를 완료하지 못했습니다. HTTPS 주소인지 확인하고 새로고침하세요. 웹 로그인은 그대로 이용할 수 있습니다.';});
    window.addEventListener('online',()=>registration?.update().catch(()=>{}));
  }
})();
