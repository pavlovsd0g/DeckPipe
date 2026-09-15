import unittest

from test_frontend_contract import run_frontend_app_probe


AUTH_FIXTURE = r'''
const accounts = {
  deezer: {connected:true,account:{id:'dz-user',name:'Deezer listener'}},
  sc: {connected:true,account:{id:'sc-user',name:'SoundCloud listener'}},
};
const commands = [];
globalThis.__invokeImpl = async (name, body) => {
  commands.push({name,body});
  if (name === 'backend_connection') return {baseUrl:'http://127.0.0.1:24680',token:'fixture-launch-token'};
  if (name === 'auth_status' && body.requestId === null) return {accounts};
  if (name === 'auth_begin') return {requestId:'reconnect-1',provider:body.provider,status:'waiting_browser'};
  if (name === 'auth_cancel') return {};
  throw new Error(`Unexpected native command: ${name}`);
};
globalThis.setTimeout = () => 1;
const configResponse = () => ({ok:true,json:async()=>({music_root:'',music_root_configured:false,wav_mode:'source',numbering:true})});
const authFailure = service => ({ok:false,status:401,json:async()=>({detail:{code:'provider_auth_required',service,message:`${service} login is required`,retryable:false}})});
globalThis.fetch = async () => configResponse();
await loadConfig();
'''


class StageDFrontendTests(unittest.TestCase):
    def run_probe(self, script):
        result = run_frontend_app_probe(AUTH_FIXTURE + script)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_provider_rejection_prompts_reconnection_after_config_refresh(self):
        self.run_probe(r'''
globalThis.fetch = async () => authFailure('deezer');
const error = await api('/api/playlists').catch(error=>error);
showError(error);
if ($('#errorRegion').textContent.includes('Защита API')) throw new Error('provider rejection blamed on local API security');
if (!$('#errorRegion').textContent.includes('Deezer') || !$('#errorRegion').textContent.includes('снова')) throw new Error('no actionable reconnect message');
globalThis.fetch = async () => configResponse();
await loadConfig();
if (!$('#btnLoginDeezer').textContent.includes('войти снова')) throw new Error('stored credential presence overwrote required reconnection');
if ($('#btnLoginSc').textContent !== 'SoundCloud: SoundCloud listener') throw new Error('unrelated provider lost its connected state');
''')

    def test_soundcloud_service_error_updates_only_soundcloud(self):
        self.run_probe(r'''
globalThis.fetch = async () => authFailure('soundcloud');
await api('/api/sc/sync-account', {body:{}}).catch(()=>{});
if (!$('#btnLoginSc').textContent.includes('войти снова')) throw new Error('soundcloud service was not mapped to its account button');
if ($('#btnLoginDeezer').textContent !== 'Deezer: Deezer listener') throw new Error('Deezer was disconnected by SoundCloud');
''')

    def test_local_launch_token_failure_does_not_invalidate_streaming_login(self):
        self.run_probe(r'''
globalThis.fetch = async () => ({ok:false,status:401,json:async()=>({detail:'Unauthorized'})});
const error = await api('/api/playlists').catch(error=>error);
if (error.kind !== 'security') throw new Error('local launch authorization lost its security error');
if ($('#btnLoginDeezer').textContent !== 'Deezer: Deezer listener') throw new Error('local API failure invalidated provider session');
''')

    def test_reconnect_opens_existing_browser_profile_without_logout(self):
        self.run_probe(r'''
globalThis.fetch = async () => authFailure('deezer');
await api('/api/playlists').catch(()=>{});
await openLogin('deezer');
if (activeAuthRequest !== 'reconnect-1' || !$('#loginResult').textContent.includes('Ожидаем')) throw new Error('reconnect button still reported connected');
if (commands.some(call=>call.name==='auth_logout')) throw new Error('reconnect cleared the saved browser profile');
if (!commands.some(call=>call.name==='auth_begin' && call.body.provider==='deezer')) throw new Error('native reconnect was not requested');
''')

    def test_successful_reconnect_restores_connected_status(self):
        self.run_probe(r'''
globalThis.fetch = async () => authFailure('deezer');
await api('/api/playlists').catch(()=>{});
await tauriLogin('deezer');
globalThis.fetch = async () => configResponse();
await renderAuthState({requestId:'reconnect-1',provider:'deezer',status:'connected',account:accounts.deezer.account});
if ($('#btnLoginDeezer').textContent !== 'Deezer: Deezer listener') throw new Error('successful reconnect left provider invalid');
''')

    def test_request_started_during_login_cannot_invalidate_new_credentials(self):
        self.run_probe(r'''
await tauriLogin('deezer');
let finishOld;
globalThis.fetch = url => new URL(url).pathname === '/api/playlists'
  ? new Promise(resolve=>{finishOld=resolve;})
  : Promise.resolve(configResponse());
const pending = api('/api/playlists').catch(error=>error);
while (!finishOld) await Promise.resolve();
await renderAuthState({requestId:'reconnect-1',provider:'deezer',status:'connected',account:accounts.deezer.account});
finishOld(authFailure('deezer'));
showError(await pending);
if ($('#btnLoginDeezer').textContent !== 'Deezer: Deezer listener') throw new Error('old HTTP rejection invalidated freshly authenticated credentials');
if ($('#errorRegion').textContent) throw new Error('old HTTP rejection displayed after successful reconnection');
''')

    def test_switching_provider_removes_old_clickable_playlists_while_loading(self):
        self.run_probe(r'''
libraryConfigured = true;
globalThis.fetch = async () => ({ok:true,json:async()=>[{id:'dz-1',title:'Deezer playlist',count:1}]});
await loadPlaylists();
let finish;
globalThis.fetch = () => new Promise(resolve=>{finish=resolve;});
tab = 'sc';
const pending = loadPlaylists();
while (!finish) await Promise.resolve();
if (collectActions($('#playlists')).length) throw new Error('Deezer playlists stayed clickable on the SoundCloud tab');
globalThis.fetch = async () => ({ok:true,json:async()=>[]});
finish({ok:true,json:async()=>({added:0,total:0,errors:[]})});
await pending;
''')

    def test_delayed_deezer_list_cannot_replace_new_local_tab(self):
        self.run_probe(r'''
libraryConfigured = true;
let finish;
globalThis.fetch = url => new URL(url).pathname === '/api/playlists'
  ? new Promise(resolve=>{finish=resolve;})
  : Promise.resolve({ok:true,json:async()=>[{id:'local-1',key:'local:local-1',title:'Local playlist',count:1}]});
const pending = loadPlaylists();
while (!finish) await Promise.resolve();
tab = 'local';
await loadPlaylists();
finish({ok:true,json:async()=>[{id:'dz-1',title:'Old Deezer playlist',count:1}]});
await pending;
if (!$('#playlists').textContent.includes('Local playlist') || $('#playlists').textContent.includes('Old Deezer')) throw new Error('old provider response replaced the selected tab');
''')

    def test_delayed_soundcloud_failure_cannot_replace_new_local_tab(self):
        self.run_probe(r'''
libraryConfigured = true;
tab = 'sc';
let finish;
globalThis.fetch = url => new URL(url).pathname === '/api/sc/sync-account'
  ? new Promise(resolve=>{finish=resolve;})
  : Promise.resolve({ok:true,json:async()=>[{id:'local-1',key:'local:local-1',title:'Local playlist',count:1}]});
const pending = loadPlaylists();
while (!finish) await Promise.resolve();
tab = 'local';
await loadPlaylists();
finish(authFailure('soundcloud'));
await pending;
const actions = collectActions($('#playlists'));
if (!actions.some(a=>a.action==='select-local-playlist') || actions.some(a=>a.action==='select-sc-source')) throw new Error('late SoundCloud response changed local controls');
if ($('#errorRegion').textContent) throw new Error('late SoundCloud failure covered the local tab');
''')

    def test_closing_success_dialog_does_not_skip_playlist_reload(self):
        self.run_probe(r'''
libraryConfigured = true;
await tauriLogin('deezer');
let finishConfig;
globalThis.fetch = url => new URL(url).pathname === '/api/config'
  ? new Promise(resolve=>{finishConfig=resolve;})
  : Promise.resolve({ok:true,json:async()=>[{id:'dz-1',title:'Fresh playlist',count:1}]});
const connected = renderAuthState({requestId:'reconnect-1',provider:'deezer',status:'connected',account:accounts.deezer.account});
while (!finishConfig) await Promise.resolve();
closeLogin();
finishConfig({ok:true,json:async()=>({music_root:'D:/Music',music_root_configured:true})});
await connected;
if (!$('#modalOverlay').classList.contains('hidden')) throw new Error('successful login reopened a closed dialog');
if (!$('#playlists').textContent.includes('Fresh playlist')) throw new Error('closing successful login skipped list recovery');
''')

    def test_create_target_completion_does_not_replace_local_tab_with_search(self):
        self.run_probe(r'''
libraryConfigured = true;
tab = 'search';
globalThis.prompt = () => 'New playlist';
let finishCreate;
globalThis.fetch = (url, options) => new URL(url).pathname === '/api/local/playlists' && options.method === 'POST'
  ? new Promise(resolve=>{finishCreate=resolve;})
  : Promise.resolve({ok:true,json:async()=>[{id:'local-1',key:'local:local-1',title:'Local playlist',count:1}]});
const creating = createTargetPlaylist('local');
while (!finishCreate) await Promise.resolve();
tab = 'local';
await loadPlaylists();
finishCreate({ok:true,json:async()=>({id:'local-2',key:'local:local-2',title:'New playlist'})});
await creating;
if (collectActions($('#playlists')).some(a=>a.action==='set-search-target')) throw new Error('late creation replaced local tab with search controls');
if (!collectActions($('#playlists')).some(a=>a.action==='select-local-playlist')) throw new Error('local tab controls disappeared');
''')

    def test_search_targets_ignore_old_provider_auth_failure_after_reconnect(self):
        self.run_probe(r'''
libraryConfigured = true;
tab = 'search';
let finishDeezer, finishConfig;
globalThis.fetch = url => {
  const path = new URL(url).pathname;
  if (path === '/api/playlists') return new Promise(resolve=>{finishDeezer=resolve;});
  if (path === '/api/config') return new Promise(resolve=>{finishConfig=resolve;});
  return Promise.resolve({ok:true,json:async()=>[]});
};
const loading = loadSearchTargets();
while (!finishDeezer) await Promise.resolve();
await tauriLogin('deezer');
const connected = renderAuthState({requestId:'reconnect-1',provider:'deezer',status:'connected',account:accounts.deezer.account});
while (!finishConfig) await Promise.resolve();
finishDeezer(authFailure('deezer'));
await loading;
if ($('#errorRegion').textContent) throw new Error('search aggregation revived old authentication failure');
finishConfig(configResponse());
await connected;
''')


if __name__ == '__main__':
    unittest.main()
