const test=require('node:test');
const assert=require('node:assert/strict');
const {captureCredential,originsFor,publicReply}=require('../../extension/helper-core.js');

function api(tab, cookie){const calls=[];return {calls,tabs:{query:async()=>[tab]},cookies:{get:async details=>{calls.push(details);return cookie;}}};}

test('Firefox cookie lookup binds exact provider and current container',async()=>{
 const browser=api({url:'https://soundcloud.com/you/library',cookieStoreId:'firefox-container-2',incognito:false},{name:'oauth_token',domain:'.soundcloud.com',path:'/',secure:true,value:'synthetic-secret'});
 assert.equal(await captureCredential(browser,'sc'),'synthetic-secret');
 assert.deepEqual(browser.calls,[{url:'https://soundcloud.com/',name:'oauth_token',storeId:'firefox-container-2'}]);
});
test('foreign origin, private tabs and missing store fail without reading cookies',async()=>{
 for(const tab of [{url:'https://soundcloud.com.evil.test',cookieStoreId:'x'},{url:'http://soundcloud.com',cookieStoreId:'x'},{url:'https://soundcloud.com',incognito:true,cookieStoreId:'x'},{url:'https://soundcloud.com'}]){
  const browser=api(tab,null);await assert.rejects(captureCredential(browser,'sc'));assert.equal(browser.calls.length,0);
 }
});
test('cookie provider mismatch and missing session reject',async()=>{
 for(const cookie of [null,{name:'arl',domain:'.soundcloud.com',value:'synthetic-secret'},{name:'oauth_token',domain:'.evil.test',value:'synthetic-secret'}]){
  await assert.rejects(captureCredential(api({url:'https://soundcloud.com/',cookieStoreId:'firefox-default'},cookie),'sc'));
 }
});
test('UI response cannot relay arbitrary fields or credentials',()=>{
 const result=publicReply({ok:true,status:{provider:'sc',status:'connected',requestId:'id',expiresIn:0,credential:'synthetic-secret',state:'secret-state',account:{id:'42',name:'User',token:'synthetic-secret'}},credential:'synthetic-secret'});
 assert.ok(!JSON.stringify(result).includes('synthetic-secret'));
 assert.ok(!JSON.stringify(result).includes('secret-state'));
 assert.throws(()=>originsFor('unknown'));
 assert.deepEqual(originsFor('sc'),['https://soundcloud.com/*']);
});
