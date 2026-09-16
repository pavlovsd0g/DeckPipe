import unittest

from qa.tests.test_frontend_contract import run_frontend_app_probe


class ReleaseUiControlsTests(unittest.TestCase):
    def probe(self, script):
        result = run_frontend_app_probe(script)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_notification_expires_and_stale_timeout_cannot_leave_page_covered(self):
        self.probe(r'''
const timers=new Map(); let serial=0;
window.setTimeout=callback=>{timers.set(++serial,callback);return serial;};
window.clearTimeout=id=>timers.delete(id);
showStatus('First'); showStatus('Second');
if(timers.size!==1) throw new Error('old notification timer was not cleared');
[...timers.values()][0]();
if(!$('#statusRegion').classList.contains('hidden') || $('#statusRegion').textContent) throw new Error('toast did not disappear');
''')

    def test_donation_opens_only_fixed_native_action_without_backend_or_url_parameter(self):
        self.probe(r'''
const calls=[];
globalThis.__invokeImpl=async(...args)=>calls.push(args);
globalThis.fetch=()=>{throw new Error('Donation must not involve music backend');};
await openDonation();
if(calls.length!==1 || calls[0][0]!=='open_donation' || calls[0].length!==1) throw new Error('unexpected external navigation permission');
''')

    def test_logs_paginate_without_duplicates_and_ignore_late_response_after_navigation(self):
        self.probe(r'''
tab='logs'; libraryConfigured=false;
const entry=id=>({id,time:'2026-09-16T12:00:00Z',level:'info',operation:'download',stage:'verified',message:'Track '+id});
const calls=[];
globalThis.fetch=async url=>{calls.push(url);return {ok:true,json:async()=>calls.length===1
  ? {entries:[entry('3'),entry('2')],has_more:true,next_before:'2'}
  : {entries:[entry('2'),entry('1')],has_more:false,next_before:null}};};
await loadLogs(); await loadLogs({append:true});
if(logsEntries.map(e=>e.id).join(',')!=='3,2,1') throw new Error('history duplicate or loss');
if(!calls[1].includes('before=2')) throw new Error('history cursor not used');
let finish; globalThis.fetch=()=>new Promise(resolve=>finish=resolve);
const pending=loadLogs(); while(!finish) await Promise.resolve();
tab='local'; $('#tracks').replaceChildren(text('Current playlist'));
finish({ok:true,json:async()=>({entries:[entry('4')],has_more:false})}); await pending;
if($('#tracks').textContent!=='Current playlist') throw new Error('late logs replaced playlist');
''')

    def test_dynamic_actions_have_help_and_reused_dialog_buttons_change_help(self):
        self.probe(r'''
for(const [action,hint] of Object.entries(BUTTON_HINTS)) {
  const control=button('Action',action);
  if(control.title!==hint) throw new Error('missing dynamic tooltip: '+action);
}
configureRbDialog({title:'Confirm',children:[],confirmLabel:'Apply',cancelLabel:'Cancel',value:true});
if($('#btnAuthStart').title!==BUTTON_HINTS['confirm-rb-dialog']) throw new Error('auth help retained on apply');
cancelRbDialog();
''')

    def test_background_playlist_refresh_preserves_paginated_log_history(self):
        self.probe(r'''
tab='logs'; logsHistory=true; logsEntries=[{id:'old'}]; $('#tracks').scrollTop=420;
let requests=0;
globalThis.fetch=async()=>{requests++;return {ok:true,json:async()=>({entries:[],has_more:false})};};
await loadPlaylists();
if(requests || !logsHistory || logsEntries[0]?.id!=='old' || $('#tracks').scrollTop!==420) throw new Error('history lost');
''')

    def test_log_reentry_keeps_refresh_available_when_initial_request_fails(self):
        self.probe(r'''
logsHistory=true; libraryConfigured=false; let poll;
window.setInterval=callback=>{poll=callback;return 1;};
globalThis.fetch=async()=>{throw new Error('offline');};
switchTab('logs');
for(let i=0;i<30;i++) await Promise.resolve();
if(!collectActions($('#tracks')).some(a=>a.action==='refresh-logs')) throw new Error('No recovery control');
if(logsHistory) throw new Error('old history blocks retry');
let called=false;
globalThis.fetch=async()=>{called=true;return {ok:true,json:async()=>({entries:[],has_more:false})};};
poll(); for(let i=0;i<30;i++) await Promise.resolve();
if(!called) throw new Error('automatic retry disabled');
''')

    def test_job_completion_does_not_interrupt_pending_older_logs(self):
        self.probe(r'''
tab='logs'; logsEntries=[{id:'new'}]; logsBefore='cursor';
let finish; let requests=0;
globalThis.fetch=async()=>{
  requests++;
  if(requests===1) return await new Promise(resolve=>finish=resolve);
  return {ok:true,json:async()=>({entries:[],has_more:false})};
};
const pending=loadLogs({append:true}); while(!finish) await Promise.resolve();
await loadPlaylists();
finish({ok:true,json:async()=>({entries:[{id:'old'}],has_more:false})}); await pending;
if(requests!==1 || !logsHistory || logsEntries.map(e=>e.id).join(',')!=='new,old') throw new Error('pending history discarded');
''')

    def test_failed_startup_scan_does_not_replace_current_logs(self):
        self.probe(r'''
let scanStarted=false; let failScan;
globalThis.fetch=async url=>{
  const path=new URL(url).pathname;
  if(path==='/api/config') return {ok:true,json:async()=>({music_root:'D:/Music',music_root_configured:true})};
  if(path==='/api/jobs') return {ok:true,json:async()=>[]};
  if(path==='/api/library/scan') {scanStarted=true;return await new Promise((resolve,reject)=>failScan=reject);}
  throw new Error('Unexpected request '+path);
};
const pending=init(); while(!scanStarted) await Promise.resolve();
tab='logs'; logsHistory=true; renderLogs();
failScan(new Error('Disk unavailable')); await pending;
if(!collectActions($('#tracks')).some(a=>a.action==='refresh-logs')) throw new Error('scan error overwrote logs');
''')

    def test_delayed_root_setup_does_not_overwrite_logs(self):
        self.probe(r'''
tab='logs'; renderLogs();
globalThis.fetch=async()=>({ok:true,json:async()=>({music_root:'',music_root_configured:false})});
await loadConfig();
if(!collectActions($('#tracks')).some(a=>a.action==='refresh-logs')) throw new Error('root setup overwrote logs');
''')

    def test_completed_queue_clear_keeps_active_download_and_never_calls_music_delete(self):
        self.probe(r'''
const active={id:'active',title:'Still downloading',state:'running',mode:'append',done:0,total:3};
const complete={id:'complete',title:'Already downloaded',state:'done',mode:'append',done:2,total:2};
renderJobs([active,complete]);
const calls=[];
globalThis.fetch=async(url,opts)=>{
  const path=new URL(url).pathname; calls.push([path,opts.method]);
  if(path==='/api/jobs/clear-completed') return {ok:true,json:async()=>({cleared:1,job_ids:['complete']})};
  if(path==='/api/jobs') return {ok:true,json:async()=>[active]};
  throw new Error('Unexpected file operation: '+path);
};
await clearCompletedJobs();
if(!$('#jobs').textContent.includes('Still downloading') || $('#jobs').textContent.includes('Already downloaded')) throw new Error('wrong jobs cleared');
if(calls[0][1]!=='POST') throw new Error('queue clear not submitted');
''')

    def test_collapse_does_not_cancel_and_poll_does_not_reopen_queue(self):
        self.probe(r'''
globalThis.fetch=()=>{throw new Error('Collapsing must not mutate jobs');};
renderJobs([{id:'a',title:'Running',state:'running',mode:'append',done:0,total:1}]);
toggleJobs();
renderJobs([{id:'a',title:'Running',state:'cancelling',mode:'append',done:0,total:1}]);
if(!$('#jobs').classList.contains('hidden')) throw new Error('poll reopened collapsed queue');
if($('#toggleJobs').getAttribute('aria-expanded')!=='false') throw new Error('collapse state unavailable');
toggleJobs();
if($('#jobs').classList.contains('hidden')) throw new Error('queue cannot reopen');
''')

    def test_late_queue_poll_cannot_restore_cleared_rows(self):
        self.probe(r'''
const complete={id:'complete',title:'Already downloaded',state:'done',mode:'append',done:2,total:2};
renderJobs([complete]);
let finish; let first=true;
globalThis.fetch=async url=>{
  if(new URL(url).pathname==='/api/jobs' && first) {first=false;return await new Promise(resolve=>finish=resolve);}
  return {ok:true,json:async()=>new URL(url).pathname==='/api/jobs' ? [] : {cleared:1}};
};
const pending=refreshJobs(); while(!finish) await Promise.resolve();
await clearCompletedJobs();
finish({ok:true,json:async()=>[complete]}); await pending;
if(queueSnapshot.length || $('#jobs').textContent.includes('Already downloaded')) throw new Error('late poll restored cleared rows');
''')

    def test_cancel_targets_only_selected_job_and_cancelled_is_terminal(self):
        self.probe(r'''
const calls=[];
const cancelled={id:'a/b',title:'Cancelled download',state:'cancelled',outcome:'cancelled',mode:'append',done:1,total:3};
globalThis.fetch=async(url,opts)=>{
  calls.push([new URL(url).pathname,opts.method]);
  return {ok:true,json:async()=>calls.length===1 ? cancelled : [cancelled]};
};
await cancelJob('a/b');
if(calls[0][0]!=='/api/jobs/a%2Fb/cancel' || calls[0][1]!=='POST') throw new Error('wrong cancel target');
if(collectActions($('#jobs')).some(a=>a.action==='cancel-job')) throw new Error('terminal job still cancellable');
if(!$('#jobs').textContent.includes('Остановлена')) throw new Error('cancelled outcome not rendered');
''')

    def test_cancelled_rekordbox_preview_never_fills_main_status_with_track_paths(self):
        self.probe(r'''
current={kind:'local',id:'local:a',title:'Set'};
const calls=[];
globalThis.fetch=async(url)=>{calls.push(url);return {ok:true,json:async()=>({
  unchanged:false,unresolved:[],error:null,plan:{hash:'a'.repeat(64),target:{id:'t',name:'Set'},
  counts:{add:1},desired:[{path:'D:/Music/'+('very-long-file-name'.repeat(30))+'.flac',position:1}]}})};};
const pending=rbSync();
await driveRbDialog({confirm:false}); await pending;
if(calls.length!==1) throw new Error('cancellation applied changes');
if($('#statusRegion').textContent.length>160 || $('#statusRegion').textContent.includes('D:/Music')) throw new Error('preview leaked into page status');
if(!$('#statusRegion').textContent.includes('отменена')) throw new Error('missing cancellation toast');
''')

    def test_additive_preview_lists_only_actual_additions_at_their_target_positions(self):
        self.probe(r'''
const result={plan:{operation_kind:'sync',add:[{position:8,title:'New membership',path:'D:/original.flac',existing_path:'D:/collection.wav'}],
  desired_resolved:[{position:1,title:'Already present',path:'D:/untouched.flac'},{position:2,title:'New membership',path:'D:/original.flac'}]}};
const preview=rbOrderedPathLines(result).join('\n');
if(!preview.includes('8. New membership') || !preview.includes('D:/collection.wav') || preview.includes('Already present') || preview.includes('D:/original.flac')) throw new Error('preview promises changes to preserved entries or paths');
''')


if __name__ == '__main__':
    unittest.main()
