'use strict';
const statusElement=document.getElementById('status');
const button=document.getElementById('connect');
let provider=null;
const errors={NO_PENDING_LOGIN:'Начните вход в DeckPipe.',DECKPIPE_NOT_RUNNING:'Откройте DeckPipe.',AUTH_PROVIDER_TAB_REQUIRED:'Выберите вкладку нужного сервиса.',AUTH_PROFILE_UNSUPPORTED:'Выберите обычную вкладку Firefox в нужном контейнере.',AUTH_LOGIN_REQUIRED:'Войдите на сайте и повторите подключение.',AUTH_PERMISSION_REQUIRED:'Разрешите доступ к выбранному сервису.',AUTH_PROVIDER_REJECTED:'Сервис не принял сессию. Повторите вход.',AUTH_EXPIRED:'Время ожидания истекло. Начните вход снова.',AUTH_CANCELLED:'Вход отменён.'};
function show(reply){
  const safe=DeckPipeHelper.publicReply(reply);
  if(!safe.ok){statusElement.textContent=errors[safe.errorCode]||'Помощник недоступен. Проверьте установку расширения и нативного моста.';button.disabled=true;return;}
  provider=safe.status?.provider;
  const connected=safe.status?.status==='connected';
  statusElement.textContent=connected?'Подключено. Вернитесь в DeckPipe.':`Подключить ${provider==='deezer'?'Deezer':'SoundCloud'}?`;
  button.disabled=connected || !provider;
}
button.addEventListener('click',async()=>{
  if(!provider)return;button.disabled=true;
  try{
    const allowed=await browser.permissions.request({origins:DeckPipeHelper.originsFor(provider)});
    if(!allowed){show({ok:false,errorCode:'AUTH_PERMISSION_REQUIRED'});return;}
    statusElement.textContent='Проверка сессии…';show(await browser.runtime.sendMessage({op:'connect'}));
  }catch{show({ok:false,errorCode:'AUTH_HELPER_UNAVAILABLE'});}
});
browser.runtime.sendMessage({op:'status'}).then(show,()=>show({ok:false,errorCode:'AUTH_HELPER_UNAVAILABLE'}));
