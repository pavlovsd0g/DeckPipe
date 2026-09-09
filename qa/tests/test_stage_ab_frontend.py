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

    def test_native_browser_auth_never_returns_credentials_to_api_login(self):
        self.run_probe(r'''
const calls = [];
globalThis.__invokeImpl = async (name,body) => {calls.push({name,body}); return {requestId:'fixture',provider:'sc',status:'connected',account:{id:'1',name:'Test user'}};};
globalThis.fetch = async url => {
  if(new URL(url).pathname.startsWith('/api/login/')) throw new Error('raw-token login API used');
  return {ok:true,json:async()=>({music_root:'',music_root_configured:false,sc_user:'Test user'})};
};
await tauriLogin('sc');
if(calls[0].name!=='auth_begin'||calls[0].body.provider!=='sc') throw new Error('default-browser broker not used');
if(calls.some(call=>call.name==='service_login')) throw new Error('embedded auth still used');
''')

    def test_native_pending_state_keeps_polling(self):
        self.run_probe(r'''
let scheduled=0;
globalThis.setTimeout=()=>{scheduled++;return 1;};
globalThis.__invokeImpl=async()=>({requestId:'pending-1',provider:'deezer',status:'waiting_browser'});
await tauriLogin('deezer');
if(activeAuthRequest!=='pending-1'||scheduled!==1) throw new Error('native waiting state was treated as terminal');
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
