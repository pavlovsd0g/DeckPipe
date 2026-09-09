'use strict';
// Privileged background only; no content scripts or website message interface.
const {captureCredential,originsFor,publicReply}=DeckPipeHelper;
let nativePort=null;
let pending=null;
let working=false;
function disconnect(){
  const port=nativePort;nativePort=null;
  if(pending){clearTimeout(pending.timer);pending.reject(new Error('AUTH_HELPER_UNAVAILABLE'));pending=null;}
  if(port)try{port.disconnect();}catch{}
}
function ensurePort(){
  if(nativePort)return;
  const port=browser.runtime.connectNative('com.deckpipe.auth');nativePort=port;
  port.onMessage.addListener(reply=>{
    if(!pending || nativePort!==port)return;
    const request=pending;pending=null;clearTimeout(request.timer);request.resolve(reply);
  });
  port.onDisconnect.addListener(()=>{if(nativePort===port)disconnect();});
}
function request(message){
  if(pending)return Promise.reject(new Error('AUTH_BUSY'));
  ensurePort();
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{disconnect();reject(new Error('AUTH_EXPIRED'));},125000);
    pending={resolve,reject,timer};
    try{nativePort.postMessage(message);}catch{disconnect();}
  });
}
async function connect(){
  const reply=await request({v:1,op:'claim_pending'});
  if(reply.ok!==true)return publicReply(reply);
  const claim=reply.claim;
  if(!claim || !['deezer','sc'].includes(claim.provider) || !/^[a-f0-9]{64}$/.test(claim.requestId) || !/^[a-f0-9]{64}$/.test(claim.state))throw new Error('AUTH_INVALID_RESPONSE');
  if(!await browser.permissions.contains({origins:originsFor(claim.provider)}))throw new Error('AUTH_PERMISSION_REQUIRED');
  let credential=await captureCredential(browser,claim.provider);
  try{return publicReply(await request({v:1,op:'complete',requestId:claim.requestId,provider:claim.provider,state:claim.state,credential}));}
  finally{credential=null;disconnect();}
}
browser.runtime.onMessage.addListener((message,sender)=>{
  if(sender.id!==browser.runtime.id || sender.url!==browser.runtime.getURL('popup.html') || !message || Object.keys(message).length!==1 || !['status','connect'].includes(message.op))return undefined;
  if(working)return Promise.resolve({ok:false,errorCode:'AUTH_BUSY'});
  working=true;
  return (async()=>{
    try{return message.op==='connect'?await connect():publicReply(await request({v:1,op:'hello'}));}
    catch(error){disconnect();return publicReply({ok:false,errorCode:error.message});}
    finally{working=false;}
  })();
});
