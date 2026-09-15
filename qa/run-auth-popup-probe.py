"""Exercise the production popup factory against an isolated synthetic site."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading


PARENT = b'''<!doctype html><title>DeckPipe synthetic popup fixture</title>
<button id="start">Test popup</button><script>
localStorage.setItem('deckpipe_probe','synthetic'); document.cookie='deckpipe_probe=synthetic; SameSite=Lax';
window.probe={}; let child, sequence=0;
document.querySelector('#start').onclick=()=>{
  child=window.open('about:blank','probe-'+(++sequence),'width=640,height=600');
  if(child) child.location='http://localhost:'+location.port+'/popup-idp';
};
addEventListener('message',e=>{
  if(e.origin!==location.origin || e.source!==child || e.data!=='fixture-ok') return;
  if(probe.keepOpen) probe.secondHandoff=true;
  else { probe.handoff=true; child.postMessage('close-fixture',location.origin);
    const timer=setInterval(()=>{if(child.closed){probe.childClosed=true;clearInterval(timer);}},50); }
});</script>'''
CHILD = b'''<!doctype html><title>DeckPipe synthetic popup child</title><script>
if(window.opener && localStorage.getItem('deckpipe_probe')==='synthetic' &&
   document.cookie.includes('deckpipe_probe=synthetic')) window.opener.postMessage('fixture-ok',location.origin);
addEventListener('message',e=>{if(e.origin===location.origin && e.source===opener && e.data==='close-fixture')window.close();});
</script>'''
IDP = b'''<!doctype html><title>DeckPipe synthetic identity provider</title><script>
location.replace('http://127.0.0.1:'+location.port+'/popup-child');
</script>'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve()
    if not root.is_relative_to(Path('D:/DeckPipe-RC-Lab').resolve()) or root.exists():
        raise ValueError('Use a new isolated evidence directory under D:/DeckPipe-RC-Lab')
    root.mkdir(parents=True)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            page=PARENT if self.path=='/popup-parent' else CHILD if self.path=='/popup-child' else IDP if self.path=='/popup-idp' else b''
            self.send_response(200 if page else 404)
            self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers(); self.wfile.write(page)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    env=dict(os.environ)
    for key, folder in [('APPDATA','Roaming'),('LOCALAPPDATA','Local'),('USERPROFILE','User'),('DECKPIPE_DATA_DIR','Data'),('TEMP','Temp'),('TMP','Temp')]:
        path=root/folder; path.mkdir(exist_ok=True); env[key]=str(path)
    output=root/'result.json'
    try:
        run=subprocess.run([str(args.exe.resolve()),'popup','--local-app-data',env['LOCALAPPDATA'],
            '--provider','sc','--url',f'http://127.0.0.1:{server.server_port}/popup-parent',
            '--result',str(output)],env=env,cwd=root,capture_output=True,timeout=60)
        (root/'synthetic-probe.log').write_bytes(run.stdout+run.stderr)
        value=json.loads(output.read_text()) if output.exists() else {'ok':False,'errorCode':'PROBE_NO_RESULT'}
        value['process_exit_code']=run.returncode
        print(json.dumps(value))
        return 0 if run.returncode==0 and value.get('ok') else 1
    finally:
        server.shutdown();server.server_close()


if __name__=='__main__':
    raise SystemExit(main())
