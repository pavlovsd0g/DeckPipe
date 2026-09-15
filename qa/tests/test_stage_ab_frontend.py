import unittest

from test_frontend_contract import run_frontend_app_probe


class StageABFrontendTests(unittest.TestCase):
    def run_probe(self, script):
        result = run_frontend_app_probe(script)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_root_is_required_before_loading_playlists(self):
        self.run_probe(r'''
const calls = [];
globalThis.fetch = async url => {
  calls.push(new URL(url).pathname);
  return {ok:true,json:async()=>({music_root:'',music_root_configured:false,numbering:true})};
};
await init();
if (calls.some(path=>path==='/api/playlists')) throw new Error('playlist sync started without a common root');
if (!$('#tracks').textContent.includes('папк')) throw new Error('root setup is not visible');
if (!collectActions($('#tracks')).some(a=>a.action==='choose-root')) throw new Error('folder picker missing');
''')

    def test_only_missing_tracks_download_and_provider_is_preserved(self):
        self.run_probe(r'''
tracks = ['ok','ambiguous','offline','error','missing'].map((status,i)=>({id:String(i),title:'Song',artist:'Artist',provider:'sc',status}));
document.querySelectorAll = selector => selector === '.trk:checked' ? tracks.map((_,i)=>({dataset:{i:String(i)}})) : [];
const selected = selectedTracks(true);
if (selected.length!==1 || selected[0].id!=='4' || selected[0].provider!=='sc') throw new Error('missing-only/provider invariant failed');
''')

    def test_location_and_ambiguity_are_visible(self):
        self.run_probe(r'''
tracks = [
 {id:'1',title:'One',artist:'Artist',status:'ok',format:'wav',file_path:'D:\\Music\\Other set\\One.wav',folder:'D:\\Music\\Other set',location_scope:'library'},
 {id:'2',title:'Two',artist:'Artist',status:'ambiguous',locations:[{path:'D:\\Music\\A.wav',folder:'D:\\Music',file:'A.wav'}]}
];
renderTracks();
if (!$('#tracks').textContent.includes('Other set')) throw new Error('actual folder not shown');
if (!collectActions($('#tracks')).some(a=>a.action==='confirm-location')) throw new Error('ambiguity choice missing');
''')

    def test_structured_provider_error_is_readable(self):
        self.run_probe(r'''
globalThis.fetch = async () => ({ok:false,status:503,json:async()=>({detail:{code:'provider_unavailable',service:'Deezer',message:'Сервис недоступен',retryable:true}})});
try { await api('/api/playlists'); throw new Error('expected failure'); }
catch(error) { if(error.message!=='Сервис недоступен') throw new Error('structured error rendered as object'); }
''')

    def test_embedded_browser_auth_never_returns_credentials_to_api_login(self):
        self.run_probe(r'''
const calls = [];
globalThis.__invokeImpl = async (name,body) => {calls.push({name,body}); return {requestId:'fixture',provider:'sc',status:'connected',account:{id:'1',name:'Test user'}};};
globalThis.fetch = async url => {
  if(new URL(url).pathname.startsWith('/api/login/')) throw new Error('raw-token login API used');
  return {ok:true,json:async()=>({music_root:'',music_root_configured:false,sc_user:'Test user'})};
};
await tauriLogin('sc');
if(calls[0].name!=='auth_begin'||calls[0].body.provider!=='sc') throw new Error('embedded browser broker not used');
if(calls.some(call=>call.name==='service_login')) throw new Error('raw credential login API used');
''')

    def test_native_pending_state_keeps_polling(self):
        self.run_probe(r'''
let scheduled=0;
globalThis.setTimeout=()=>{scheduled++;return 1;};
globalThis.__invokeImpl=async()=>({requestId:'pending-1',provider:'deezer',status:'waiting_browser'});
await tauriLogin('deezer');
if(activeAuthRequest!=='pending-1'||scheduled!==1) throw new Error('native waiting state was treated as terminal');
''')

    def test_cancelled_attempt_stops_polling_and_uses_one_cancel_command(self):
        self.run_probe(r'''
let scheduled;
let clearCount=0;
const calls=[];
globalThis.setTimeout=callback=>{scheduled=callback;return 73;};
globalThis.clearTimeout=()=>{clearCount++;};
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_begin') return {requestId:'cancel-1',provider:'deezer',status:'waiting_browser'};
  if(name==='auth_status') throw new Error('cancelled attempt polled again');
  return {};
};
await tauriLogin('deezer');
closeLogin();
await Promise.resolve();
if(calls.filter(call=>call.name==='auth_cancel').length!==1) throw new Error('cancel command was not invoked exactly once');
if(clearCount!==1) throw new Error('cancelled attempt left its polling timer active');
await scheduled();
if(calls.some(call=>call.name==='auth_status')) throw new Error('late timer polled a cancelled attempt');
''')

    def test_embedded_popup_notice_stays_nonterminal(self):
        self.run_probe(r'''
let scheduled=0;
globalThis.setTimeout=()=>{scheduled++;return 1;};
globalThis.__invokeImpl=async()=>({requestId:'popup-1',provider:'deezer',status:'waiting_browser',errorCode:'AUTH_POPUP_BLOCKED'});
await tauriLogin('deezer');
const message=$('#loginResult').textContent;
if(!message.includes('Дополнительное окно входа не открылось')) throw new Error('popup policy notice missing');
if(message.includes('не удалось')) throw new Error('popup policy was rendered as terminal failure');
if(scheduled!==1||activeAuthRequest!=='popup-1') throw new Error('popup policy stopped a pending attempt');
''')

    def test_transient_prepare_failure_retries_with_a_fresh_begin(self):
        self.run_probe(r'''
const calls=[];
let begin=0;
globalThis.setTimeout=()=>1;
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_begin') return {requestId:`retry-${++begin}`,provider:'sc',status:'waiting_browser',errorCode:'AUTH_BACKEND_UNAVAILABLE'};
  return {};
};
await tauriLogin('sc');
if($('#btnAuthRetry').classList.contains('hidden')) throw new Error('retry remains hidden after transient preparation failure');
await retryAuthLogin();
if(calls.filter(call=>call.name==='auth_begin').length!==2) throw new Error('retry did not start a fresh embedded attempt');
if(activeAuthRequest!=='retry-2') throw new Error('retry did not replace the active attempt');
''')

    def test_late_cancelled_attempt_cannot_replace_new_account_state(self):
        self.run_probe(r'''
let resolveOld;
const oldResult=new Promise(resolve=>{resolveOld=resolve;});
globalThis.setTimeout=()=>1;
globalThis.__invokeImpl=async(name,body)=>{
  if(name!=='auth_begin') return {};
  if(body.provider==='deezer') return oldResult;
  return {requestId:'sc-new',provider:'sc',status:'waiting_browser'};
};
const oldLogin=tauriLogin('deezer');
await Promise.resolve();
await tauriLogin('sc');
resolveOld({requestId:'dz-old',provider:'deezer',status:'connected',account:{name:'Stale Deezer'}});
await oldLogin;
if(activeAuthRequest!=='sc-new') throw new Error('late response replaced active request');
if($('#loginResult').textContent.includes('Stale Deezer')) throw new Error('late response rendered a connected stale account');
''')

    def test_late_open_login_status_cannot_reopen_a_closed_dialog(self):
        self.run_probe(r'''
let resolveStatus;
const pendingStatus=new Promise(resolve=>{resolveStatus=resolve;});
globalThis.__invokeImpl=async(name,body)=>{
  if(name==='auth_status'&&body.requestId===null) return pendingStatus;
  return {};
};
const opening=openLogin('deezer');
closeLogin();
resolveStatus({accounts:{deezer:{connected:true,account:{name:'Late Deezer'}}}});
await opening;
if(!$('#modalOverlay').classList.contains('hidden')) throw new Error('late account status reopened a closed dialog');
''')

    def test_late_open_login_status_rejection_is_ignored_after_close(self):
        self.run_probe(r'''
let rejectStatus;
const pendingStatus=new Promise((resolve,reject)=>{rejectStatus=reject;});
globalThis.__invokeImpl=async(name,body)=>name==='auth_status'&&body.requestId===null ? pendingStatus : {};
clearError();
const opening=openLogin('deezer');
closeLogin();
rejectStatus(new Error('stale status rejected'));
await opening;
if(!$('#modalOverlay').classList.contains('hidden')) throw new Error('late status rejection reopened the dialog');
if($('#errorRegion').textContent) throw new Error('late status rejection surfaced after dialog close');
''')

    def test_late_open_login_status_cannot_replace_a_new_provider_dialog(self):
        self.run_probe(r'''
let resolveStatus;
const pendingStatus=new Promise(resolve=>{resolveStatus=resolve;});
globalThis.__invokeImpl=async(name,body)=>{
  if(name==='auth_status'&&body.requestId===null) return pendingStatus;
  return {};
};
const opening=openLogin('deezer');
await Promise.resolve();
renderLoginDialog('sc');
authAttemptEpoch++;
resolveStatus({accounts:{deezer:{connected:true,account:{name:'Late Deezer'}}}});
await opening;
if($('#loginTitle').textContent.includes('Deezer')) throw new Error('late account status replaced the newer provider dialog');
''')

    def test_late_begin_response_after_close_cannot_render_connected(self):
        self.run_probe(r'''
let resolveBegin;
const pendingBegin=new Promise(resolve=>{resolveBegin=resolve;});
globalThis.setTimeout=()=>1;
globalThis.__invokeImpl=async(name)=>name==='auth_begin'
  ? pendingBegin
  : {};
const starting=tauriLogin('deezer');
await Promise.resolve();
closeLogin();
resolveBegin({requestId:'late-close',provider:'deezer',status:'connected',account:{name:'Late Deezer'}});
await starting;
if($('#loginResult').textContent.includes('Late Deezer')) throw new Error('late begin response rendered a closed dialog as connected');
if(!$('#modalOverlay').classList.contains('hidden')) throw new Error('late begin response reopened the dialog');
''')

    def test_late_begin_rejection_cannot_override_replacement_attempt(self):
        self.run_probe(r'''
let rejectOld;
const pendingOld=new Promise((resolve,reject)=>{rejectOld=reject;});
globalThis.setTimeout=()=>1;
globalThis.__invokeImpl=async(name,body)=>{
  if(name==='auth_begin'&&body.provider==='deezer') return pendingOld;
  if(name==='auth_begin') return {requestId:'sc-new',provider:'sc',status:'waiting_browser'};
  return {};
};
clearError();
const oldLogin=tauriLogin('deezer');
await Promise.resolve();
await tauriLogin('sc');
rejectOld(new Error('stale begin rejected'));
await oldLogin;
if(activeAuthRequest!=='sc-new') throw new Error('late rejection replaced the active request');
if($('#errorRegion').textContent) throw new Error('late rejection was rendered over the replacement attempt');
''')

    def test_late_begin_rejection_cannot_surface_after_dialog_close(self):
        self.run_probe(r'''
let rejectBegin;
const pendingBegin=new Promise((resolve,reject)=>{rejectBegin=reject;});
globalThis.__invokeImpl=async(name)=>name==='auth_begin' ? pendingBegin : {};
clearError();
const starting=tauriLogin('deezer');
await Promise.resolve();
closeLogin();
rejectBegin(new Error('stale begin rejected'));
await starting;
if(!$('#modalOverlay').classList.contains('hidden')) throw new Error('late rejection reopened the dialog');
if($('#errorRegion').textContent) throw new Error('late rejection surfaced after dialog close');
''')

    def test_change_account_forgets_selected_session_before_new_begin(self):
        self.run_probe(r'''
const calls=[];
globalThis.setTimeout=()=>1;
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_begin') return {requestId:'sc-new',provider:'sc',status:'waiting_browser'};
  return {};
};
await changeAuthAccount();
const logout=calls.findIndex(call=>call.name==='auth_logout');
const begin=calls.findIndex(call=>call.name==='auth_begin');
if(logout<0||begin<0||logout>begin) throw new Error('account switch did not forget the selected session before a new login');
if(calls[logout].body.provider!=='sc') throw new Error('account switch forgot another provider');
''')

    def test_change_account_surfaces_forget_failure_without_starting_new_login(self):
        self.run_probe(r'''
const calls=[];
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_logout') throw new Error('AUTH_BROWSER_CLEAR_FAILED');
  if(name==='auth_begin') throw new Error('new login must not start after forget failure');
  return {};
};
await changeAuthAccount();
if(!$('#errorRegion').textContent.includes('Не удалось выйти и забыть вход')) throw new Error('forget failure was hidden behind a generic action rejection');
if(calls.some(call=>call.name==='auth_begin')) throw new Error('new login started after forget failure');
''')

    def test_delayed_account_switch_completion_after_close_does_not_reopen_login(self):
        self.run_probe(r'''
const calls=[];
let releaseLogout;
let logoutStarted;
const delayedLogout=new Promise(resolve=>{releaseLogout=resolve;});
const started=new Promise(resolve=>{logoutStarted=resolve;});
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
renderLoginDialog('sc');
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_logout') { logoutStarted(); return delayedLogout; }
  if(name==='auth_begin') return {requestId:'new-'+body.provider,provider:body.provider,status:'waiting_browser'};
  return {};
};
const switching=changeAuthAccount();
await started;
closeLogin();
releaseLogout({});
await switching;
if(!$('#modalOverlay').classList.contains('hidden')) throw new Error('closed switch reopened the dialog');
if(calls.some(call=>call.name==='auth_begin')) throw new Error('closed switch began an obsolete provider login');
''')

    def test_delayed_account_switch_completion_cannot_cancel_or_replace_new_provider(self):
        self.run_probe(r'''
const calls=[];
let releaseLogout;
let logoutStarted;
const delayedLogout=new Promise(resolve=>{releaseLogout=resolve;});
const started=new Promise(resolve=>{logoutStarted=resolve;});
globalThis.setTimeout=()=>1;
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
renderLoginDialog('sc');
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_logout') { logoutStarted(); return delayedLogout; }
  if(name==='auth_begin') return {requestId:'new-'+body.provider,provider:body.provider,status:'waiting_browser'};
  return {};
};
const switching=changeAuthAccount();
await started;
closeLogin();
await tauriLogin('deezer');
releaseLogout({});
await switching;
if(activeAuthRequest!=='new-deezer') throw new Error('old switch replaced the newer request');
if(calls.some(call=>call.name==='auth_cancel'&&call.body.requestId==='new-deezer')) throw new Error('old switch cancelled the newer request');
if(calls.filter(call=>call.name==='auth_begin').some(call=>call.body.provider==='sc')) throw new Error('old switch restarted SC after Deezer replacement');
''')

    def test_delayed_logout_completion_cannot_close_new_provider_dialog(self):
        self.run_probe(r'''
const calls=[];
let releaseLogout;
let logoutStarted;
const delayedLogout=new Promise(resolve=>{releaseLogout=resolve;});
const started=new Promise(resolve=>{logoutStarted=resolve;});
globalThis.setTimeout=()=>1;
loadConfig=async()=>({});
loadPlaylists=async()=>{};
libraryConfigured=false;
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
renderLoginDialog('sc');
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_logout') { logoutStarted(); return delayedLogout; }
  if(name==='auth_begin') return {requestId:'new-'+body.provider,provider:body.provider,status:'waiting_browser'};
  return {};
};
const loggingOut=logoutProvider();
await started;
closeLogin();
await tauriLogin('deezer');
releaseLogout({});
await loggingOut;
if($('#modalOverlay').classList.contains('hidden')) throw new Error('old logout closed the newer Deezer dialog');
if(activeAuthRequest!=='new-deezer') throw new Error('old logout replaced the newer request');
''')

    def test_delayed_forget_failure_after_replacement_is_silent_and_does_not_restart_old_provider(self):
        self.run_probe(r'''
const calls=[];
let rejectLogout;
let logoutStarted;
const delayedLogout=new Promise((resolve,reject)=>{rejectLogout=reject;});
const started=new Promise(resolve=>{logoutStarted=resolve;});
globalThis.setTimeout=()=>1;
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
renderLoginDialog('sc');
clearError();
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_logout') { logoutStarted(); return delayedLogout; }
  if(name==='auth_begin') return {requestId:'new-'+body.provider,provider:body.provider,status:'waiting_browser'};
  return {};
};
const switching=changeAuthAccount();
await started;
closeLogin();
await tauriLogin('deezer');
rejectLogout(new Error('AUTH_BROWSER_CLEAR_FAILED'));
await switching;
if($('#errorRegion').textContent) throw new Error('stale forget failure was rendered over the new provider');
if(calls.filter(call=>call.name==='auth_begin').some(call=>call.body.provider==='sc')) throw new Error('stale forget failure restarted SC');
if(activeAuthRequest!=='new-deezer') throw new Error('stale forget failure replaced the newer request');
''')

    def test_delayed_logout_failure_after_replacement_does_not_hide_new_dialog_or_error(self):
        self.run_probe(r'''
const calls=[];
let rejectLogout;
let logoutStarted;
const delayedLogout=new Promise((resolve,reject)=>{rejectLogout=reject;});
const started=new Promise(resolve=>{logoutStarted=resolve;});
globalThis.setTimeout=()=>1;
loginService='sc';
nativeAccounts={sc:{connected:true,account:{name:'Old SC'}}};
renderLoginDialog('sc');
clearError();
globalThis.__invokeImpl=async(name,body)=>{
  calls.push({name,body});
  if(name==='auth_logout') { logoutStarted(); return delayedLogout; }
  if(name==='auth_begin') return {requestId:'new-'+body.provider,provider:body.provider,status:'waiting_browser'};
  return {};
};
const loggingOut=logoutProvider();
await started;
closeLogin();
await tauriLogin('deezer');
rejectLogout(new Error('AUTH_BROWSER_CLEAR_FAILED'));
await loggingOut;
if($('#modalOverlay').classList.contains('hidden')) throw new Error('stale logout failure hid the new dialog');
if($('#errorRegion').textContent) throw new Error('stale logout failure was rendered over the new provider');
if(activeAuthRequest!=='new-deezer') throw new Error('stale logout failure replaced the newer request');
''')

    def test_partial_remote_write_is_visible_separately_from_download(self):
        self.run_probe(r'''
globalThis.setInterval=()=>1;
try { showDownloadResult({job_id:'job-1',already_present:2,remote_action:{state:'failed',last_error:{message:'Deezer недоступен'}}}); }
catch(error) { throw new Error('download/remote result UI missing: '+error.message); }
if(!$('#statusRegion').textContent.includes('2')) throw new Error('reused local tracks not shown');
if(!$('#errorRegion').textContent.includes('Deezer')) throw new Error('remote failure hidden');
''')

    def test_sync_does_not_claim_complete_with_unresolved_tracks(self):
        self.run_probe(r'''
libraryConfigured=libraryReady=true;
tracks=[{id:'1',status:'ambiguous',title:'Song',provider:'sc'}];
await syncAppend();
if($('#statusRegion').textContent.includes('Всё уже скачано')) throw new Error('ambiguous playlist declared complete');
if(!$('#statusRegion').textContent.includes('1')) throw new Error('unresolved count missing');
''')

    def test_incomplete_error_inventory_does_not_render_no_errors(self):
        self.run_probe(r'''
globalThis.fetch=async url=>({ok:true,json:async()=>new URL(url).pathname==='/api/errors'
  ? {items:[],errors:[{message:'Deezer временно недоступен'}]} : []});
await loadErrors();
if($('#playlists').textContent.includes('Ошибок нет')) throw new Error('partial inventory treated as complete');
if(!$('#errorRegion').textContent.includes('Deezer')) throw new Error('inventory failure invisible');
''')

    def test_soundcloud_order_preserves_provider_qualified_sidecar_ids(self):
        self.run_probe(r'''
libraryConfigured=libraryReady=true;
current={kind:'sc',id:'likes',title:'Likes'};
tracks=[{id:'42',provider:'sc',title:'Song',status:'ok'}];
rescan=async()=>{};
let sent;
globalThis.fetch=async(url,options)=>{
  if(new URL(url).pathname.endsWith('/renumber')) sent=JSON.parse(options.body).order;
  return {ok:true,json:async()=>({renamed:0,state:'ready',configured:true,tracks:[]})};
};
await syncPlaylistOrder();
if(sent?.[0]!=='sc:42') throw new Error('SoundCloud order lost sidecar namespace');
''')


if __name__ == '__main__':
    unittest.main()
