import json
import unittest

from qa.tests.test_frontend_contract import run_frontend_app_probe


class StageCFrontendBehaviorTests(unittest.TestCase):
    maxDiff = None

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
  globalThis.confirm = () => scenario.confirm;
  current = {kind:'local', id:'local:fixture', title:'Set'};
  await rbSync();
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
globalThis.confirm = message => { confirms.push(message); return true; };
globalThis.prompt = () => { throw new Error('technical token prompt must not be shown'); };
current = {kind:'local', id:'local:empty', title:'Новый пустой'};
await rbSync();
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
];
const calls=[];
globalThis.fetch=async(url,request)=>{
  calls.push({url,body:request.body ? JSON.parse(request.body) : null});
  return {ok:true,json:async()=>responses.shift()};
};
globalThis.prompt=message=>message.includes('2.') ? '2' : null;
globalThis.confirm=()=>{throw new Error('unchanged target must not ask to apply');};
current={kind:'local',id:'local:dup',title:'Дубль'};
await rbSync();
console.log(JSON.stringify({calls,status:elements.get('#statusRegion').textContent}));
"""
        )
        self.assertEqual(len(payload["calls"]), 3)
        self.assertRegex(payload["calls"][1]["url"], r"/api/rb/status$")
        self.assertEqual(payload["calls"][2]["body"]["playlist_id"], "22")
        self.assertRegex(payload["calls"][2]["url"], r"dry_run=true$")

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
  globalThis.confirm=()=>scenario === 'prepare-fails';
  current={kind:'local',id:'local:set',title:'Set'};
  await flipWav();
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
  globalThis.confirm=()=>true;
  current={kind:'local',id:'local:set',title:'Set'};
  await flipWav();
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
  globalThis.confirm=()=>true;
  current={kind:'local',id:'local:set',title:'Set'};
  await rbSync();
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
for (const scenario of ['cancel','restore','vanished']) {
  const calls=[];
  const responses=[
    {dry_run:true,applied:false,reconciled:false,unchanged:false,recovery:true,unresolved:[],
      error:{code:'recovery_needed',message:'pending'},plan:{hash:'t'.repeat(64),counts:{add:1}}},
  ];
  if (scenario === 'restore') responses.push(
    {dry_run:false,applied:false,reconciled:false,unchanged:false,recovery:true,unresolved:[],backup_id:'journal-1',
      error:{code:'recovery_restored_preview_required',message:'restored'},plan:{hash:'t'.repeat(64)}},
    {dry_run:true,applied:false,reconciled:false,unchanged:true,recovery:false,unresolved:[],error:null,
      plan:{hash:'u'.repeat(64),target:{id:'rb-1',name:'Set'},counts:{},desired_resolved:[],shared_content:[]}}
  );
  if (scenario === 'vanished') responses.push(
    {dry_run:false,applied:false,reconciled:false,unchanged:false,recovery:false,unresolved:[],
      error:{code:'stale_preview',message:'stale'},plan:{hash:'v'.repeat(64)}}
  );
  globalThis.fetch=async(url,request)=>{
    calls.push({url,body:JSON.parse(request.body)});
    return {ok:true,json:async()=>responses.shift()};
  };
  const confirms=[];
  globalThis.confirm=message=>{confirms.push(message);return scenario !== 'cancel';};
  current={kind:'local',id:'local:set',title:'Set'};
  await rbSync();
  runs.push({scenario,calls,confirms,status:elements.get('#statusRegion').textContent,error:elements.get('#errorRegion').textContent});
}
console.log(JSON.stringify(runs));
"""
        )
        cancelled, restored, vanished = payload
        self.assertEqual(len(cancelled["calls"]), 1)
        self.assertTrue(cancelled["confirms"], cancelled)
        self.assertIn("восстанов", cancelled["confirms"][0].lower())
        self.assertEqual(len(restored["calls"]), 3)
        self.assertEqual(restored["calls"][1]["body"]["expected_plan_hash"], "t" * 64)
        self.assertRegex(restored["calls"][2]["url"], r"dry_run=true$")
        self.assertIn("восстанов", restored["status"].lower())
        self.assertEqual(len(vanished["calls"]), 2)
        self.assertNotIn("восстановлена", vanished["status"].lower())
        self.assertTrue(vanished["error"])

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


if __name__ == "__main__":
    unittest.main()
