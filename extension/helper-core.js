(function (root) {
  'use strict';
  const providers=Object.freeze({
    deezer:{hosts:['www.deezer.com','deezer.com'],cookie:'arl',origins:['https://www.deezer.com/*','https://deezer.com/*']},
    sc:{hosts:['soundcloud.com'],cookie:'oauth_token',origins:['https://soundcloud.com/*']}
  });
  function definition(provider){if(!Object.hasOwn(providers,provider))throw new Error('AUTH_INVALID_PROVIDER');return providers[provider];}
  function originsFor(provider){return [...definition(provider).origins];}
  async function captureCredential(api,provider){
    const selected=definition(provider);
    const tabs=await api.tabs.query({active:true,currentWindow:true});
    if(tabs.length!==1)throw new Error('AUTH_PROVIDER_TAB_REQUIRED');
    const tab=tabs[0];
    if(tab.incognito || typeof tab.cookieStoreId!=='string' || !tab.cookieStoreId)throw new Error('AUTH_PROFILE_UNSUPPORTED');
    let url;try{url=new URL(tab.url);}catch{throw new Error('AUTH_PROVIDER_TAB_REQUIRED');}
    if(url.protocol!=='https:' || url.port || url.username || url.password || !selected.hosts.includes(url.hostname))throw new Error('AUTH_PROVIDER_TAB_REQUIRED');
    const cookie=await api.cookies.get({url:url.origin+'/',name:selected.cookie,storeId:tab.cookieStoreId});
    if(!cookie)throw new Error('AUTH_LOGIN_REQUIRED');
    if(cookie.name!==selected.cookie || !selected.hosts.includes(cookie.domain.replace(/^\./,'')) || cookie.path!=='/' || !cookie.secure || typeof cookie.value!=='string' || !cookie.value || cookie.value.length>8192 || /[\x00-\x1f\x7f]/.test(cookie.value))throw new Error('AUTH_COOKIE_REJECTED');
    return cookie.value;
  }
  function publicReply(reply){
    const result={ok:reply?.ok===true};
    const allowed=['NO_PENDING_LOGIN','DECKPIPE_NOT_RUNNING','AUTH_HOST_SOURCE_REJECTED','AUTH_PIPE_UNAVAILABLE','AUTH_PEER_REJECTED','AUTH_HELPER_UNAVAILABLE','AUTH_INVALID_MESSAGE','AUTH_INVALID_RESPONSE','AUTH_PROVIDER_REJECTED','AUTH_PROVIDER_TAB_REQUIRED','AUTH_PROFILE_UNSUPPORTED','AUTH_LOGIN_REQUIRED','AUTH_COOKIE_REJECTED','AUTH_PERMISSION_REQUIRED','AUTH_BUSY','AUTH_EXPIRED','AUTH_CANCELLED','AUTH_INVALID_COMPLETION'];
    if(!result.ok){result.errorCode=allowed.includes(reply?.errorCode)?reply.errorCode:'AUTH_HELPER_UNAVAILABLE';return result;}
    const status=reply.status;
    if(status && Object.hasOwn(providers,status.provider)){
      result.status={provider:status.provider,requestId:String(status.requestId||'').slice(0,128),status:['waiting_browser','waiting_helper','validating','connected','cancelled','expired','failed'].includes(status.status)?status.status:'failed',expiresIn:Math.max(0,Math.min(300,Number(status.expiresIn)||0))};
      if(status.account)result.status.account={id:String(status.account.id||'').slice(0,128),name:String(status.account.name||'').slice(0,256)};
    }
    return result;
  }
  const api={captureCredential,originsFor,publicReply};
  if(typeof module==='object' && module.exports)module.exports=api;
  else root.DeckPipeHelper=api;
})(globalThis);
