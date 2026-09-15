import json
import unittest

from qa.tests.test_frontend_contract import run_frontend_app_probe


class StageCFrontendBehaviorTests(unittest.TestCase):
    maxDiff = None

    def test_global_recovery_is_actionable_without_source_or_library(self):
        payload = self.probe(r'''
current = null; libraryConfigured = libraryReady = false;
const calls=[];
globalThis.fetch=async(url,request)=>{
  const path=new URL(url).pathname;
  calls.push({url,body:request.body ? JSON.parse(request.body) : null});
  if(path==='/api/rb/status') return {ok:true,json:async()=>({running:false,recovery:{needed:true}})};
  if(path!=='/api/rb/recovery') throw new Error('unexpected source dependency: '+path);
  return {ok:true,json:async()=>request.body
    ? {error:{code:'recovery_restored_preview_required'}}
    : {error:null,recovery:true,plan:{hash:'r'.repeat(64),operation_id:'operation-A',phase:'database_committed'}}};
};
const operation=rbRecovery();
await driveRbDialog({confirm:true});
await operation;
console.log(JSON.stringify({calls,status:$('#statusRegion').textContent,busy:rbOperationBusy,current}));
''')
        self.assertIsNone(payload['current'])
        self.assertFalse(payload['busy'])
        self.assertEqual(len(payload['calls']), 3)
        self.assertEqual(payload['calls'][2]['body'], {'expected_plan_hash':'r'*64})
        self.assertIn('RESTORE_REKORDBOX_OPERATION', payload['calls'][2]['url'])
        self.assertIn('восстановлена', payload['status'])

    def test_deleted_target_is_forgotten_only_for_affected_source_before_fresh_preview(self):
        payload = self.probe(r'''
current={kind:'local',id:'local:a',title:'A'};
const sourceA=captureRbSelection();
const sourceB={key:'local:b',title:'B'};
rememberRbTarget(sourceA,'deleted'); rememberRbTarget(sourceB,'keep');
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push(JSON.parse(request.body));
  return {ok:true,json:async()=>calls.length===1
    ? {error:{code:'playlist_not_found'},plan:null}
    : {error:null,unchanged:true,plan:{hash:'new',target:{id:'fresh',name:'A'},counts:{}}}};
};
await rbSync();
const afterFailure=rememberedRbTarget(sourceA);
await rbSync();
console.log(JSON.stringify({calls,afterFailure,other:rememberedRbTarget(sourceB),fresh:rememberedRbTarget(sourceA)}));
''')
        self.assertEqual(payload['calls'][0]['playlist_id'], 'deleted')
        self.assertNotIn('playlist_id', payload['calls'][1])
        self.assertIsNone(payload['afterFailure'])
        self.assertEqual((payload['other'],payload['fresh']), ('keep','fresh'))

    def test_partial_empty_absent_and_disjoint_membership_never_prepare_wav(self):
        payload = self.probe(r'''
const results=[];
for(const [mode,membership] of [['original','partial'],['wav','partial'],['empty','partial'],['empty','absent_target'],['blocked','mismatch'],['original','order_mismatch']]) {
  current={kind:'local',id:'local:set',title:'Set'};
  const calls=[];
  globalThis.prompt=()=>{throw new Error('false mixed-format prompt');};
  globalThis.fetch=async(url)=>{calls.push(url);return {ok:true,json:async()=>({error:null,unresolved:[],media_state:{mode,membership}})};};
  await flipWav();
  results.push({calls,status:$('#statusRegion').textContent,dialog:!!pendingRbDialog});
}
console.log(JSON.stringify(results));
''')
        for result in payload:
            self.assertEqual(len(result['calls']), 1)
            self.assertFalse(result['dialog'])
            self.assertIn('Сначала синхронизируйте', result['status'])

    def probe(self, script):
        result = run_frontend_app_probe(script)
        self.assertEqual(result.returncode, 0, result.stdout)
        return json.loads(result.stdout)

    def test_sync_cancellation_unresolved_and_missing_hash_never_apply(self):
        payload = self.probe(
            r"""
const scenarios = [
  {
    name: 'cancelled',
    preview: {dry_run:true, applied:false, reconciled:false, unchanged:false, unresolved:[], error:null,
      plan:{hash:'a'.repeat(64), target:{id:'rb-1',name:'Set'}, counts:{add:1}, desired_resolved:[], shared_content:[]}},
    confirm: false,
  },
  {
    name: 'unresolved',
    preview: {dry_run:true, applied:false, reconciled:false, unchanged:false,
      unresolved:[{code:'catalog_missing',position:1}], error:{code:'source_unresolved',message:'refused'}, plan:null},
    confirm: true,
  },
  {
    name: 'missing-hash',
    preview: {dry_run:true, applied:false, reconciled:false, unchanged:false, unresolved:[], error:null,
      plan:{target:{id:'rb-1',name:'Set'}, counts:{add:1}, desired_resolved:[], shared_content:[]}},
    confirm: true,
  },
];
const outcomes = [];
for (const scenario of scenarios) {
  const calls = [];
  globalThis.fetch = async (url, request) => {
    calls.push({url, body: JSON.parse(request.body)});
    return {ok:true, json:async()=>scenario.preview};
  };
  globalThis.confirm = () => { throw new Error('native confirm must not be used'); };
  current = {kind:'local', id:'local:fixture', title:'Set'};
  const operation = rbSync();
  await driveRbDialog({confirm: scenario.confirm});
  await operation;
  outcomes.push({name:scenario.name, calls, error:elements.get('#errorRegion').textContent});
}
console.log(JSON.stringify(outcomes));
"""
        )
        for outcome in payload:
            self.assertEqual(len(outcome["calls"]), 1, outcome)
            self.assertRegex(outcome["calls"][0]["url"], r"/api/rb/sync\?dry_run=true$")
        self.assertIn("не все треки", payload[1]["error"].lower())
        self.assertTrue(payload[2]["error"])

    def test_sync_forwards_exact_displayed_hash_and_applies_new_empty_target(self):
        payload = self.probe(
            r"""
const viewedHash = 'b'.repeat(64);
const responses = [
  {dry_run:true, applied:false, reconciled:false, unchanged:false, unresolved:[], error:null,
    plan:{hash:viewedHash, target:{id:null,name:'Новый пустой'}, counts:{desired:0,current:0,resolved:0,add:0,remove:0,reorder:0,metadata:0,path:0,unresolved:0},
      desired_resolved:[], shared_content:[]}, media_state:{mode:'empty'}},
  {dry_run:false, applied:true, reconciled:true, unchanged:false, unresolved:[], error:null, backup_id:'backup-1',
    plan:{hash:viewedHash, target:{id:null,name:'Новый пустой'}, counts:{}}, media_state:{mode:'empty'}},
];
const calls = [];
const confirms = [];
globalThis.fetch = async (url, request) => {
  calls.push({url, body:JSON.parse(request.body)});
  return {ok:true, json:async()=>responses.shift()};
};
globalThis.confirm = () => { throw new Error('native confirm must not be used'); };
globalThis.prompt = () => { throw new Error('technical token prompt must not be shown'); };
current = {kind:'local', id:'local:empty', title:'Новый пустой'};
const operation = rbSync();
for(let i=0;i<40 && !pendingRbDialog;i++) await Promise.resolve();
confirms.push(elements.get('#loginSteps').textContent);
confirmRbDialog();
await operation;
console.log(JSON.stringify({calls, confirms, status:elements.get('#statusRegion').textContent}));
"""
        )
        self.assertEqual(len(payload["calls"]), 2)
        self.assertEqual(
            payload["calls"][1]["body"],
            {
                "playlist_key": "local:empty",
                "playlist_title": "Новый пустой",
                "expected_plan_hash": "b" * 64,
            },
        )
        self.assertRegex(payload["calls"][1]["url"], r"dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES$")
        self.assertIn("Новый пустой", payload["confirms"][0])
        self.assertNotIn("hash", payload["confirms"][0].lower())
        self.assertNotIn("APPLY_REKORDBOX_CHANGES", payload["confirms"][0])
        self.assertIn("применены", payload["status"].lower())

    def test_stale_selection_and_duplicate_click_cannot_apply_old_preview(self):
        payload = self.probe(
            r"""
let releaseFirst;
const first = new Promise(resolve => { releaseFirst = resolve; });
const calls = [];
globalThis.fetch = async (url, request) => {
  calls.push({url, body:JSON.parse(request.body)});
  if (calls.length === 1) return {ok:true, json:async()=>first};
  return {ok:true, json:async()=>({dry_run:true,applied:false,reconciled:false,unchanged:true,unresolved:[],error:null,
    plan:{hash:'n'.repeat(64),target:{id:'rb-1',name:'First'},counts:{},desired_resolved:[],shared_content:[]}})};
};
let confirms = 0;
globalThis.confirm = () => { confirms++; return true; };
loadPlaylists = async()=>{};
loadTracks = async()=>{};
current = {kind:'deezer', id:'first', title:'First'};
const pending = rbSync();
const duplicate = rbSync();
await selectPlaylist('second', 'Second');
await selectPlaylist('first', 'First');
releaseFirst({dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
  plan:{hash:'o'.repeat(64),target:{id:'rb-1',name:'First'},counts:{add:1},desired_resolved:[],shared_content:[]}});
await Promise.all([pending, duplicate]);
await rbSync();
console.log(JSON.stringify({calls, confirms, status:elements.get('#statusRegion').textContent}));
"""
        )
        self.assertEqual(len(payload["calls"]), 2, payload)
        self.assertEqual(payload["confirms"], 0, payload)
        self.assertRegex(payload["calls"][1]["url"], r"dry_run=true$")

    def test_ambiguous_target_is_selected_by_readable_index_then_repreviewed(self):
        payload = self.probe(
            r"""
const responses = [
  {dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],
    error:{code:'ambiguous_playlist_target',message:'ambiguous'},plan:null},
  {db_exists:true,running:false,playlists:[{id:'11',name:'Дубль',count:2},{id:'22',name:'Дубль',count:5}]},
  {dry_run:true,applied:false,reconciled:false,unchanged:true,unresolved:[],error:null,
    plan:{hash:'p'.repeat(64),target:{id:'22',name:'Дубль'},counts:{},desired_resolved:[],shared_content:[]}},
  {error:null,unresolved:[],media_state:{mode:'wav'}},
  {dry_run:true,applied:false,reconciled:false,unchanged:true,unresolved:[],error:null,
    plan:{hash:'k'.repeat(64),target:{id:'22',name:'Дубль'},counts:{},desired_resolved:[],shared_content:[]},media_state:{mode:'wav'}},
];
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push({url,body:request.body ? JSON.parse(request.body) : null});
  return {ok:true,json:async()=>responses.shift()};
};
globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
current={kind:'local',id:'local:dup',title:'Дубль'};
const operation=rbSync();
await driveRbDialog({targetId:'22'});
await operation;
await flipWav();
console.log(JSON.stringify({calls,status:elements.get('#statusRegion').textContent}));
"""
        )
        self.assertEqual(len(payload["calls"]), 5)
        self.assertRegex(payload["calls"][1]["url"], r"/api/rb/status$")
        self.assertEqual(payload["calls"][2]["body"]["playlist_id"], "22")
        self.assertRegex(payload["calls"][2]["url"], r"dry_run=true$")
        self.assertIn("playlist_id=22", payload["calls"][3]["url"])
        self.assertEqual(payload["calls"][4]["body"]["playlist_id"], "22")

    def test_wav_uses_persisted_media_state_and_preparation_failure_blocks_flip(self):
        payload = self.probe(
            r"""
const runs=[];
for (const scenario of ['prepare-fails','persisted-wav']) {
  const calls=[];
  const responses = scenario === 'prepare-fails' ? [
    {dry_run:true,error:null,unresolved:[],plan:{},media_state:{mode:'original'}},
    {prepared:0,reused:0,total:2,state:'blocked',tracks:[],unresolved:[],error:{code:'wav_preparation_failed',message:'failed'}},
  ] : [
    {dry_run:true,error:null,unresolved:[],plan:{},media_state:{mode:'wav'}},
    {dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
      plan:{hash:'r'.repeat(64),target:{id:'rb-9',name:'Set'},counts:{path:2},desired_resolved:[
        {position:1,title:'One',path:'D:/Music/One.flac'},{position:2,title:'Two',path:'D:/Music/Two.flac'}],shared_content:[]},media_state:{mode:'wav'}},
  ];
  globalThis.fetch=async(url,request)=>{
    calls.push({url,body:request.body ? JSON.parse(request.body) : null});
    return {ok:true,json:async()=>responses.shift()};
  };
  globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
  current={kind:'local',id:'local:set',title:'Set'};
  const operation=flipWav();
  await driveRbDialog({confirm:scenario === 'prepare-fails'});
  await operation;
  runs.push({scenario,calls,error:elements.get('#errorRegion').textContent});
}
console.log(JSON.stringify(runs));
"""
        )
        failed, reverted = payload
        self.assertEqual(len(failed["calls"]), 2)
        self.assertRegex(failed["calls"][0]["url"], r"/api/rb/media-state\?")
        self.assertRegex(failed["calls"][1]["url"], r"/api/rb/prepare-wav$")
        self.assertNotIn("/api/flip", " ".join(call["url"] for call in failed["calls"]))
        self.assertTrue(failed["error"])
        self.assertEqual(len(reverted["calls"]), 2)
        self.assertRegex(reverted["calls"][1]["url"], r"/api/flip\?dry_run=true$")
        self.assertFalse(reverted["calls"][1]["body"]["to_wav"])

    def test_wav_prepare_preview_apply_and_revert_apply_exact_viewed_hashes(self):
        payload = self.probe(
            r"""
const runs=[];
for (const direction of ['wav','original']) {
  const calls=[];
  const viewedHash=(direction === 'wav' ? 'x' : 'y').repeat(64);
  const initialMode=direction === 'wav' ? 'original' : 'wav';
  const responses=[{error:null,unresolved:[],media_state:{mode:initialMode}}];
  if (direction === 'wav') responses.push(
    {prepared:2,reused:0,total:2,state:'prepared',tracks:[],unresolved:[],error:null},
    {error:null,unresolved:[],media_state:{mode:'original',prepared:2,total:2}}
  );
  responses.push(
    {dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
      plan:{hash:viewedHash,target:{id:'rb-1',name:'Set'},counts:{path:2},desired_resolved:[
        {position:1,title:'One',path:direction === 'wav' ? 'D:/Music/One.wav' : 'D:/Music/One.flac'}],shared_content:[]},
      media_state:{mode:initialMode}},
    {dry_run:false,applied:true,reconciled:true,unchanged:false,unresolved:[],error:null,backup_id:'backup',
      plan:{hash:viewedHash},media_state:{mode:direction === 'wav' ? 'wav' : 'original'}}
  );
  globalThis.fetch=async(url,request)=>{
    calls.push({url,body:request.body ? JSON.parse(request.body) : null});
    return {ok:true,json:async()=>responses.shift()};
  };
  globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
  current={kind:'local',id:'local:set',title:'Set'};
  const operation=flipWav();
  await driveRbDialog();
  if(direction === 'wav') await driveRbDialog();
  await operation;
  runs.push({direction,calls,status:elements.get('#statusRegion').textContent});
}
console.log(JSON.stringify(runs));
"""
        )
        to_wav, to_original = payload
        self.assertEqual(
            [new_url.split("/api/")[1].split("?")[0] for new_url in [call["url"] for call in to_wav["calls"]]],
            ["rb/media-state", "rb/prepare-wav", "rb/media-state", "flip", "flip"],
        )
        self.assertTrue(to_wav["calls"][-1]["body"]["to_wav"])
        self.assertEqual(to_wav["calls"][-1]["body"]["expected_plan_hash"], "x" * 64)
        self.assertIn("применены", to_wav["status"].lower())
        self.assertNotIn("prepare-wav", " ".join(call["url"] for call in to_original["calls"]))
        self.assertFalse(to_original["calls"][-1]["body"]["to_wav"])
        self.assertEqual(to_original["calls"][-1]["body"]["expected_plan_hash"], "y" * 64)
        self.assertIn("применены", to_original["status"].lower())

    def test_unreconciled_apply_is_not_success_but_committed_state_error_stays_truthful(self):
        payload = self.probe(
            r"""
const outcomes=[];
for (const applied of [
  {dry_run:false,applied:false,reconciled:false,unchanged:false,unresolved:[],error:{code:'reconcile_failed',message:'failed'},plan:{},media_state:{mode:'blocked'}},
  {dry_run:false,applied:true,reconciled:true,unchanged:false,unresolved:[],error:{code:'media_state_unavailable',message:'state failed'},plan:{},media_state:{mode:'blocked',error:{code:'media_state_unavailable'}}},
  {dry_run:false,applied:true,reconciled:true,unchanged:false,unresolved:[],error:{code:'callback_failed',message:'callback failed'},plan:{},media_state:{mode:'original'}},
]) {
  const responses=[{dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
    plan:{hash:'s'.repeat(64),target:{id:'rb-1',name:'Set'},counts:{add:1},desired_resolved:[],shared_content:[]}},applied];
  globalThis.fetch=async()=>({ok:true,json:async()=>responses.shift()});
  globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
  current={kind:'local',id:'local:set',title:'Set'};
  const operation=rbSync();
  await driveRbDialog();
  await operation;
  outcomes.push({status:elements.get('#statusRegion').textContent,error:elements.get('#errorRegion').textContent});
}
console.log(JSON.stringify(outcomes));
"""
        )
        self.assertNotIn("успеш", payload[0]["status"].lower())
        self.assertTrue(payload[0]["error"])
        self.assertIn("применены", payload[1]["status"].lower())
        self.assertIn("состояни", payload[1]["status"].lower())
        self.assertIn("применены", payload[2]["status"].lower())
        self.assertIn("дополнительная обработка", payload[2]["status"].lower())
        self.assertTrue(payload[2]["error"])

    def test_recovery_requires_separate_confirmation_then_fresh_normal_preview(self):
        payload = self.probe(
            r"""
const runs=[];
for (const scenario of ['cancel','restore','vanished','changed-cancel','changed-apply']) {
  const calls=[];
  const responses=[
    {dry_run:true,applied:false,reconciled:false,unchanged:false,recovery:true,unresolved:[],
      error:{code:'recovery_needed',message:'pending'},plan:{hash:'t'.repeat(64),counts:{add:1}}},
    {dry_run:true,recovery:true,error:null,plan:{hash:'t'.repeat(64),operation_id:'operation-A',phase:'database_committed'}},
  ];
  if (['restore','changed-cancel','changed-apply'].includes(scenario)) responses.push(
    {dry_run:false,applied:false,reconciled:false,unchanged:false,recovery:true,unresolved:[],backup_id:'journal-1',
      error:{code:'recovery_restored_preview_required',message:'restored'},plan:{hash:'t'.repeat(64)}},
    {dry_run:true,applied:false,reconciled:false,unchanged:scenario === 'restore',recovery:false,unresolved:[],error:null,
      plan:{hash:'u'.repeat(64),target:{id:'rb-1',name:'Set'},counts:{add:scenario === 'restore' ? 0 : 1},desired_resolved:[],shared_content:[]}}
  );
  if (scenario === 'changed-apply') responses.push(
    {dry_run:false,applied:true,reconciled:true,unchanged:false,recovery:false,unresolved:[],error:null,backup_id:'new-backup',
      plan:{hash:'u'.repeat(64)},media_state:{mode:'original'}}
  );
  if (scenario === 'vanished') responses.push(
    {dry_run:false,applied:false,reconciled:false,unchanged:false,recovery:false,unresolved:[],
      error:{code:'stale_preview',message:'stale'},plan:{hash:'v'.repeat(64)}}
  );
  globalThis.fetch=async(url,request)=>{
    calls.push({url,body:request.body ? JSON.parse(request.body) : null});
    return {ok:true,json:async()=>responses.shift()};
  };
  const confirms=[];
  globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
  async function settle(value){
    for(let i=0;i<40 && !pendingRbDialog;i++) await Promise.resolve();
    if(!pendingRbDialog) return false;
    confirms.push({title:elements.get('#loginTitle').textContent,message:elements.get('#loginSteps').textContent});
    if(value) confirmRbDialog(); else cancelRbDialog();
    return true;
  }
  current={kind:'local',id:'local:set',title:'Set'};
  const operation=rbSync();
  await settle(scenario !== 'cancel');
  if(scenario.startsWith('changed-')) await settle(scenario === 'changed-apply');
  await operation;
  runs.push({scenario,calls,confirms,status:elements.get('#statusRegion').textContent,error:elements.get('#errorRegion').textContent});
}
console.log(JSON.stringify(runs));
"""
        )
        cancelled, restored, vanished, changed_cancel, changed_apply = payload
        self.assertEqual(len(cancelled["calls"]), 2)
        self.assertTrue(cancelled["confirms"], cancelled)
        self.assertIn("восстанов", cancelled["confirms"][0]["message"].lower())
        self.assertEqual(len(restored["calls"]), 4)
        self.assertEqual(restored["calls"][2]["body"]["expected_plan_hash"], "t" * 64)
        self.assertRegex(restored["calls"][3]["url"], r"dry_run=true$")
        self.assertIn("восстанов", restored["status"].lower())
        self.assertEqual(len(vanished["calls"]), 3)
        self.assertNotIn("восстановлена", vanished["status"].lower())
        self.assertTrue(vanished["error"])
        self.assertEqual(len(changed_cancel["calls"]), 4)
        self.assertEqual(len(changed_cancel["confirms"]), 2)
        self.assertIn("Восстановление", changed_cancel["confirms"][0]["title"])
        self.assertIn("синхронизации", changed_cancel["confirms"][1]["title"].lower())
        self.assertIn("отменено", changed_cancel["status"].lower())
        self.assertEqual(len(changed_apply["calls"]), 5)
        self.assertEqual(len(changed_apply["confirms"]), 2)
        self.assertEqual(changed_apply["calls"][2]["body"]["expected_plan_hash"], "t" * 64)
        self.assertEqual(changed_apply["calls"][4]["body"]["expected_plan_hash"], "u" * 64)
        self.assertIn("применены", changed_apply["status"].lower())

    def test_preview_names_target_and_shows_ordered_paths_counts_and_shared_effect(self):
        payload = self.probe(
            r"""
const preview = formatRbPreview({dry_run:true,unchanged:false,unresolved:[],error:null,
  plan:{hash:'w'.repeat(64),target:{id:'rb-7',name:'Сет'},counts:{add:1,remove:2,reorder:3,metadata:4,path:5,unresolved:0},
    desired_resolved:[
      {position:2,title:'Two',path:'D:/Library/Two.wav'},
      {position:1,title:'One',path:'D:/Library/One.flac'},
    ],shared_content:[{content_id:'c1',playlist_ids:['rb-7','rb-8']}]},
  media_state:{mode:'mixed'}}, 'Проверка');
console.log(JSON.stringify({preview}));
"""
        )["preview"]
        self.assertIn("существующий плейлист «Сет»", payload)
        self.assertIn("ID rb-7", payload)
        self.assertIn("добавить: 1", payload)
        self.assertIn("1. One — D:/Library/One.flac", payload)
        self.assertIn("2. Two — D:/Library/Two.wav", payload)
        self.assertLess(payload.index("1. One — D:/Library/One.flac"), payload.index("2. Two — D:/Library/Two.wav"))
        self.assertIn("Общие треки: 1", payload)
        self.assertIn("2 связей", payload)
        self.assertIn("смешанный", payload)

    def test_local_tab_loads_canonical_source_without_streaming_login(self):
        payload = self.probe(
            r"""
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push(url);
  const path=new URL(url).pathname;
  if (path==='/api/local/playlists') return {ok:true,json:async()=>[{key:'local:fixture',title:'Офлайн',count:1}]};
  if (path==='/api/playlists/local:fixture/tracks') return {ok:true,json:async()=>({path:'D:/Music/Offline',tracks:[
    {id:'1',provider:'deezer',title:'One',artist:'A',album:'',duration:120,status:'ok',file_path:'D:/Music/One.flac'}
  ]})};
  if (path==='/api/rb/media-state') return {ok:true,json:async()=>({error:{code:'adapter_open_failed'},media_state:{mode:'blocked'}})};
  throw new Error('unexpected route '+path);
};
libraryConfigured=true;
libraryReady=true;
tab='local';
await loadPlaylists();
const actions=collectActions(elements.get('#playlists'));
await selectLocalPlaylist('local:fixture','Офлайн');
console.log(JSON.stringify({calls,actions,current,status:elements.get('#statusRegion').textContent,title:elements.get('#pltitle').textContent}));
"""
        )
        self.assertEqual(payload["current"]["id"], "local:fixture")
        self.assertEqual(payload["title"], "Офлайн")
        self.assertIn("select-local-playlist", [item["action"] for item in payload["actions"]])
        joined = " ".join(payload["calls"])
        self.assertIn("/api/local/playlists", joined)
        self.assertIn("/api/playlists/local:fixture/tracks", joined)
        self.assertNotRegex(joined, r"/api/(?:deezer|sc|config|auth)")

    def test_duplicate_name_wav_chooses_exact_target_before_state_and_flip_preview(self):
        payload = self.probe(
            r"""
const responses=[
  {error:{code:'ambiguous_playlist_target'},unresolved:[],media_state:{mode:'blocked'}},
  {db_exists:true,running:false,playlists:[{id:'11',name:'Дубль',count:2},{id:'22',name:'Дубль',count:2}]},
  {error:null,unresolved:[],media_state:{mode:'wav'}},
  {dry_run:true,applied:false,reconciled:false,unchanged:true,unresolved:[],error:null,
    plan:{hash:'z'.repeat(64),target:{id:'22',name:'Дубль'},counts:{},desired_resolved:[],shared_content:[]},media_state:{mode:'wav'}},
];
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push({url,body:request.body ? JSON.parse(request.body) : null});
  return {ok:true,json:async()=>responses.shift()};
};
globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
current={kind:'local',id:'local:dup',title:'Дубль'};
const pending=flipWav();
for(let i=0;i<20 && !(typeof pendingRbDialog !== 'undefined' && pendingRbDialog);i++) await Promise.resolve();
const dialogAvailable=typeof confirmRbDialog === 'function' && !!pendingRbDialog;
let choiceText='';
if(dialogAvailable){
  choiceText=elements.get('#rbTargetSelect').textContent;
  elements.get('#rbTargetSelect').value='22';
  confirmRbDialog();
}
await pending;
console.log(JSON.stringify({dialogAvailable,choiceText,calls,status:elements.get('#statusRegion').textContent,error:elements.get('#errorRegion').textContent}));
"""
        )
        self.assertTrue(payload["dialogAvailable"], payload)
        self.assertIn("ID 11", payload["choiceText"])
        self.assertIn("ID 22", payload["choiceText"])
        self.assertEqual(len(payload["calls"]), 4, payload)
        self.assertRegex(payload["calls"][1]["url"], r"/api/rb/status$")
        self.assertIn("playlist_id=22", payload["calls"][2]["url"])
        self.assertEqual(payload["calls"][3]["body"]["playlist_id"], "22")
        self.assertIn("ID 22", payload["status"])

    def test_duplicate_name_original_target_keeps_id_through_prepare_and_flip_preview(self):
        payload = self.probe(
            r"""
const responses=[
  {error:{code:'ambiguous_playlist_target'},unresolved:[],media_state:{mode:'blocked'}},
  {db_exists:true,running:false,playlists:[{id:'11',name:'Дубль',count:2},{id:'22',name:'Дубль',count:2}]},
  {error:null,unresolved:[],media_state:{mode:'original'}},
  {prepared:2,reused:0,total:2,state:'prepared',tracks:[],unresolved:[],error:null},
  {error:null,unresolved:[],media_state:{mode:'original',prepared:2,total:2}},
  {dry_run:true,applied:false,reconciled:false,unchanged:true,unresolved:[],error:null,
    plan:{hash:'j'.repeat(64),target:{id:'22',name:'Дубль'},counts:{},desired_resolved:[],shared_content:[]},media_state:{mode:'original'}},
];
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push({url,body:request.body ? JSON.parse(request.body) : null});
  return {ok:true,json:async()=>responses.shift()};
};
globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
current={kind:'local',id:'local:dup',title:'Дубль'};
const pending=flipWav();
await driveRbDialog({targetId:'22'});
await driveRbDialog();
await pending;
console.log(JSON.stringify({calls,status:elements.get('#statusRegion').textContent}));
"""
        )
        self.assertEqual(len(payload["calls"]), 6, payload)
        self.assertIn("playlist_id=22", payload["calls"][2]["url"])
        self.assertRegex(payload["calls"][3]["url"], r"/api/rb/prepare-wav$")
        self.assertIn("playlist_id=22", payload["calls"][4]["url"])
        self.assertEqual(payload["calls"][5]["body"]["playlist_id"], "22")
        self.assertTrue(payload["calls"][5]["body"]["to_wav"])
        self.assertIn("ID 22", payload["status"])

    def test_delayed_preview_cannot_replace_new_auth_dialog_or_status(self):
        payload = self.probe(
            r"""
let releasePreview;
const previewPromise=new Promise(resolve=>{releasePreview=resolve;});
let calls=0;
globalThis.fetch=async()=>{
  calls++;
  return {ok:true,json:async()=>previewPromise};
};
globalThis.__invokeImpl=async(name,body)=>{
  if(name==='backend_connection') return {baseUrl:'http://127.0.0.1:24680',token:'test'};
  if(name==='auth_status'&&body.requestId===null) return {accounts:{deezer:{connected:true,account:{name:'DJ'}}}};
  return {};
};
let nativeConfirms=0;
globalThis.confirm=()=>{nativeConfirms++;return false;};
current={kind:'local',id:'local:set',title:'Set'};
const pending=rbSync();
await Promise.resolve();
await openLogin('deezer');
showStatus('AUTH-MARKER');
releasePreview({dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
  plan:{hash:'q'.repeat(64),target:{id:'rb-1',name:'Set'},counts:{add:1},desired_resolved:[],shared_content:[]}});
await pending;
console.log(JSON.stringify({calls,nativeConfirms,status:elements.get('#statusRegion').textContent,
  title:elements.get('#loginTitle').textContent,visible:!elements.get('#modalOverlay').classList.contains('hidden')}));
"""
        )
        self.assertEqual(payload["nativeConfirms"], 0, payload)
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["status"], "AUTH-MARKER")
        self.assertEqual(payload["title"], "Вход в Deezer")
        self.assertTrue(payload["visible"])

    def test_closing_rekordbox_dialog_cancels_and_unlocks_without_apply(self):
        payload = self.probe(
            r"""
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push(url);
  return {ok:true,json:async()=>({dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
    plan:{hash:'d'.repeat(64),target:{id:'rb-1',name:'Set'},counts:{add:1},desired_resolved:[],shared_content:[]}})};
};
globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
current={kind:'local',id:'local:set',title:'Set'};
const pending=rbSync();
for(let i=0;i<20 && !(typeof pendingRbDialog !== 'undefined' && pendingRbDialog);i++) await Promise.resolve();
const dialogAvailable=typeof pendingRbDialog !== 'undefined' && !!pendingRbDialog;
if(dialogAvailable) closeLogin();
await pending;
console.log(JSON.stringify({dialogAvailable,calls,busy:rbOperationBusy,visible:!elements.get('#modalOverlay').classList.contains('hidden'),
  status:elements.get('#statusRegion').textContent}));
"""
        )
        self.assertTrue(payload["dialogAvailable"], payload)
        self.assertEqual(len(payload["calls"]), 1)
        self.assertFalse(payload["busy"])
        self.assertFalse(payload["visible"])
        self.assertIn("отменено", payload["status"].lower())

    def test_playlist_change_settles_and_closes_stale_rekordbox_dialog(self):
        payload = self.probe(
            r"""
const calls=[];
globalThis.fetch=async url=>{
  calls.push(url);
  return {ok:true,json:async()=>({dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
    plan:{hash:'c'.repeat(64),target:{id:'rb-1',name:'First'},counts:{add:1},desired_resolved:[],shared_content:[]}})};
};
globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
loadPlaylists=async()=>{};
loadTracks=async()=>{};
current={kind:'deezer',id:'first',title:'First'};
const operation=rbSync();
for(let i=0;i<40 && !pendingRbDialog;i++) await Promise.resolve();
await selectPlaylist('second','Second');
const afterSelection={visible:!elements.get('#modalOverlay').classList.contains('hidden'),pending:!!pendingRbDialog,busy:rbOperationBusy};
if(pendingRbDialog) cancelRbDialog();
await operation;
console.log(JSON.stringify({calls,afterSelection,current}));
"""
        )
        self.assertEqual(len(payload["calls"]), 1)
        self.assertEqual(payload["current"]["id"], "second")
        self.assertFalse(payload["afterSelection"]["visible"], payload)
        self.assertFalse(payload["afterSelection"]["pending"], payload)
        self.assertFalse(payload["afterSelection"]["busy"], payload)

    def test_auth_replacement_settles_open_rekordbox_dialog_without_status_takeover(self):
        payload = self.probe(
            r"""
let calls=0;
globalThis.fetch=async()=>{
  calls++;
  return {ok:true,json:async()=>({dry_run:true,applied:false,reconciled:false,unchanged:false,unresolved:[],error:null,
    plan:{hash:'e'.repeat(64),target:{id:'rb-1',name:'Set'},counts:{add:1},desired_resolved:[],shared_content:[]}})};
};
globalThis.__invokeImpl=async(name,body)=>{
  if(name==='backend_connection') return {baseUrl:'http://127.0.0.1:24680',token:'test'};
  if(name==='auth_status'&&body.requestId===null) return {accounts:{deezer:{connected:true,account:{name:'DJ'}}}};
  return {};
};
globalThis.confirm=()=>{throw new Error('native confirm must not be used');};
current={kind:'local',id:'local:set',title:'Set'};
const pending=rbSync();
for(let i=0;i<40 && !pendingRbDialog;i++) await Promise.resolve();
const rbWasVisible=activeDialogKind==='rb'&&!elements.get('#modalOverlay').classList.contains('hidden');
await openLogin('deezer');
showStatus('AUTH-REPLACED');
await pending;
console.log(JSON.stringify({calls,rbWasVisible,busy:rbOperationBusy,status:elements.get('#statusRegion').textContent,
  title:elements.get('#loginTitle').textContent,kind:activeDialogKind,visible:!elements.get('#modalOverlay').classList.contains('hidden')}));
"""
        )
        self.assertTrue(payload["rbWasVisible"], payload)
        self.assertEqual(payload["calls"], 1)
        self.assertFalse(payload["busy"])
        self.assertEqual(payload["status"], "AUTH-REPLACED")
        self.assertEqual(payload["title"], "Вход в Deezer")
        self.assertEqual(payload["kind"], "auth")
        self.assertTrue(payload["visible"])

    def test_stale_track_failure_cannot_replace_new_selection_table(self):
        payload = self.probe(
            r"""
let rejectFirst;
const firstPromise=new Promise((resolve,reject)=>{rejectFirst=reject;});
globalThis.fetch=async url=>{
  const path=new URL(url).pathname;
  if(path==='/api/playlists/first/tracks') return {ok:true,json:async()=>firstPromise};
  if(path==='/api/playlists/second/tracks') return {ok:true,json:async()=>({path:'D:/Second',tracks:[
    {id:'2',provider:'deezer',title:'Second Track',artist:'B',album:'',duration:120,status:'ok',file_path:'D:/Second.flac'}]})};
  if(path==='/api/rb/media-state') return {ok:true,json:async()=>({error:{code:'adapter_open_failed'},media_state:{mode:'blocked'}})};
  throw new Error('unexpected '+path);
};
const first={kind:'deezer',id:'first',title:'First'};
libraryConfigured=true;
current=first;
const stale=loadTracks(first);
await Promise.resolve();
invalidateRbOperation();
const second={kind:'deezer',id:'second',title:'Second'};
current=second;
await loadTracks(second);
rejectFirst(new Error('Old first request failed'));
await stale;
console.log(JSON.stringify({current,title:elements.get('#pltitle').textContent,tracksText:elements.get('#tracks').textContent,
  error:elements.get('#errorRegion').textContent}));
"""
        )
        self.assertEqual(payload["current"]["id"], "second")
        self.assertEqual(payload["title"], "Second")
        self.assertIn("Second Track", payload["tracksText"])
        self.assertNotIn("Old first request failed", payload["tracksText"])
        self.assertNotIn("Old first request failed", payload["error"])

    def test_opening_auth_does_not_discard_current_playlist_track_load(self):
        payload = self.probe(
            r"""
let releaseTracks;
const pendingTracks=new Promise(resolve=>{releaseTracks=resolve;});
globalThis.fetch=async url=>{
  const path=new URL(url).pathname;
  if(path==='/api/playlists/current/tracks') return {ok:true,json:async()=>pendingTracks};
  if(path==='/api/rb/media-state') return {ok:true,json:async()=>({error:{code:'adapter_open_failed'},media_state:{mode:'blocked'}})};
  throw new Error('unexpected '+path);
};
globalThis.__invokeImpl=async(name,body)=>{
  if(name==='backend_connection') return {baseUrl:'http://127.0.0.1:24680',token:'test'};
  if(name==='auth_status'&&body.requestId===null) return {accounts:{deezer:{connected:true,account:{name:'DJ'}}}};
  return {};
};
libraryConfigured=true;
current={kind:'deezer',id:'current',title:'Current'};
const loading=loadTracks(current);
await Promise.resolve();
await openLogin('deezer');
releaseTracks({path:'D:/Current',tracks:[
  {id:'7',provider:'deezer',title:'Current Track',artist:'DJ',album:'',duration:180,status:'ok',file_path:'D:/Current.flac'}]});
await loading;
console.log(JSON.stringify({title:elements.get('#pltitle').textContent,tracksText:elements.get('#tracks').textContent,
  dialogTitle:elements.get('#loginTitle').textContent,visible:!elements.get('#modalOverlay').classList.contains('hidden')}));
"""
        )
        self.assertEqual(payload["title"], "Current")
        self.assertIn("Current Track", payload["tracksText"])
        self.assertEqual(payload["dialogTitle"], "Вход в Deezer")
        self.assertTrue(payload["visible"])


if __name__ == "__main__":
    unittest.main()
