import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
APP_STATIC = ROOT / "app" / "static"
DESKTOP_UI = ROOT / "desktop" / "ui"
ASSETS = ("index.html", "app.js", "styles.css")


def read_text(path):
    return path.read_text(encoding="utf-8")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_blob_bytes(repo_path, repo_root=None):
    root = ROOT if repo_root is None else Path(repo_root)
    result = subprocess.run(
        ["git", "show", f"HEAD:{repo_path}"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def git_archive_blob_bytes(repo_paths, repo_root=None):
    root = ROOT if repo_root is None else Path(repo_root)
    result = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "--", *repo_paths],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    blobs = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for repo_path in repo_paths:
            try:
                member = archive.getmember(repo_path)
            except KeyError as error:
                raise AssertionError(f"git archive HEAD did not include {repo_path}") from error
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AssertionError(f"git archive HEAD member {repo_path} is not a file")
            blobs[repo_path] = extracted.read()
    return blobs


def run_frontend_transport_probe(probe_script):
    source = read_text(FRONTEND / "app.js")
    transport_only = source.split("async function loadConfig", 1)[0]
    transport_only = re.sub(
        r"import\s+\{\s*invoke\s*\}\s+from\s+['\"]@tauri-apps/api/core['\"]\s*;",
        "const invoke = async name => { globalThis.__invokeCalls.push(name); return globalThis.__invokeResult; };",
        transport_only,
        count=1,
    )
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "transport-probe.mjs"
        probe.write_text(transport_only + "\n" + probe_script, encoding="utf-8")
        result = subprocess.run(
            ["node", str(probe)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    return result


def run_frontend_action_probe(probe_script):
    source = read_text(FRONTEND / "app.js")
    source = re.sub(
        r"import\s+\{\s*invoke\s*\}\s+from\s+['\"]@tauri-apps/api/core['\"]\s*;",
        "const invoke = async () => ({baseUrl: 'http://127.0.0.1:24680', token: 'packaged-token'});",
        source,
        count=1,
    )
    source = source.replace("\ninit();\n", "\n// init skipped by frontend action probe\n")
    prelude = r"""
class FakeClassList {
  constructor(owner) { this.owner = owner; this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); this.sync(); }
  remove(...names) { names.forEach(name => this.values.delete(name)); this.sync(); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : !!force;
    if (enabled) this.values.add(name); else this.values.delete(name);
    this.sync();
    return enabled;
  }
  sync() { this.owner.className = [...this.values].join(' '); }
}
class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.className = '';
    this.classList = new FakeClassList(this);
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this._textContent = '';
  }
  set id(value) {
    this._id = String(value);
    if (globalThis.__elementMap) globalThis.__elementMap.set(`#${this._id}`, this);
  }
  get id() { return this._id || ''; }
  set textContent(value) { this._textContent = String(value ?? ''); }
  get textContent() {
    return this._textContent + this.children.map(child => child && child.textContent !== undefined ? child.textContent : '').join('');
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  append(...items) { this.children.push(...items.flat().filter(item => item !== null && item !== undefined)); }
  replaceChildren(...items) { this.children = []; this._textContent = ''; this.append(...items); }
  querySelectorAll() { return []; }
  closest() { return null; }
  matches() { return false; }
  addEventListener() {}
  focus() {}
  remove() { this.removed = true; }
}
const elements = new Map();
globalThis.__elementMap = elements;
function fixture(selector) {
  const node = new FakeElement(selector.replace(/^[#.]/, '') || 'div');
  if (selector === '#modalOverlay') node.classList.add('hidden');
  elements.set(selector, node);
  return node;
}
[
  '#statusRegion', '#errorRegion', '#modalOverlay', '#modal', '#searchInput',
  '#tracks', '#playlists', '#toolbar', '#pltitle', '#plpath', '#plstats',
  '#flipBtn', '#searchFilters'
].forEach(fixture);
globalThis.window = {location: {protocol: 'tauri:', origin: 'http://tauri.localhost'}};
globalThis.document = {
  activeElement: null,
  querySelector(selector) { return elements.get(selector) || fixture(selector); },
  querySelectorAll() { return []; },
  getElementById(id) { return elements.get(`#${id}`) || null; },
  createElement(tag) { return new FakeElement(tag); },
  createTextNode(value) { const node = new FakeElement('#text'); node.textContent = value; return node; },
  addEventListener() {},
};
function collectActions(node, out = []) {
  if (!node) return out;
  if (node.dataset && node.dataset.action) out.push(node.dataset.action);
  for (const child of node.children || []) collectActions(child, out);
  return out;
}
"""
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "action-probe.mjs"
        probe.write_text(prelude + "\n" + source + "\n" + probe_script, encoding="utf-8")
        return subprocess.run(
            ["node", str(probe)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


class FrontendBuildContractTests(unittest.TestCase):
    maxDiff = None

    def assertNoInlineCspHazards(self, html, js, css):
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)", "HTML must not contain inline script tags")
        self.assertNotRegex(html, r"<style(?:\s|>)", "HTML must not contain inline style tags")
        self.assertNotRegex(html, r"\sstyle\s*=", "HTML must not contain style attributes")
        self.assertNotRegex(html, r"\son[a-z]+\s*=", "HTML must not contain event handler attributes")
        self.assertNotRegex(html, r"javascript\s*:", "HTML must not contain javascript: URLs")
        combined = "\n".join([html, js, css])
        self.assertNotRegex(combined, r"sourceMappingURL|['\"][^'\"]+\.(?:js|css)\.map['\"]", "Assets must not disclose source maps")
        self.assertNotRegex(js, r"\.setAttribute\(\s*['\"](?:style|on[a-z]+)['\"]")
        self.assertNotRegex(js, r"\.style(?:\.|\[)", "JavaScript must not manufacture inline style attributes")
        self.assertNotRegex(js, r"javascript\s*:")

    def run_temp_build(self, output_root):
        app_out = output_root / "app" / "static"
        desktop_out = output_root / "desktop" / "ui"
        result = subprocess.run(
            [
                "node",
                "frontend/build.mjs",
                "--app-static",
                str(app_out),
                "--desktop-ui",
                str(desktop_out),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        return app_out, desktop_out, result.stdout

    def test_canonical_source_and_generated_outputs_exist(self):
        for path in [
            FRONTEND / "index.html",
            FRONTEND / "app.js",
            FRONTEND / "styles.css",
            FRONTEND / "build.mjs",
            ROOT / "package-lock.json",
        ]:
            self.assertTrue(path.is_file(), f"missing {path.relative_to(ROOT)}")
        for name in ASSETS:
            self.assertTrue((APP_STATIC / name).is_file(), f"missing app/static/{name}")
            self.assertTrue((DESKTOP_UI / name).is_file(), f"missing desktop/ui/{name}")

    def test_build_outputs_are_identical_and_tracked_outputs_have_no_manual_drift(self):
        with tempfile.TemporaryDirectory() as td:
            app_out, desktop_out, _ = self.run_temp_build(Path(td))
            for name in ASSETS:
                self.assertEqual(
                    (app_out / name).read_bytes(),
                    (desktop_out / name).read_bytes(),
                    f"temporary app/static/{name} and desktop/ui/{name} differ",
                )
                self.assertEqual(
                    (app_out / name).read_bytes(),
                    (APP_STATIC / name).read_bytes(),
                    f"tracked app/static/{name} drifted from canonical build",
                )
                self.assertEqual(
                    (desktop_out / name).read_bytes(),
                    (DESKTOP_UI / name).read_bytes(),
                    f"tracked desktop/ui/{name} drifted from canonical build",
                )

    def test_generated_asset_blobs_are_archive_stable_on_windows(self):
        expected_paths = [
            f"frontend/{name}" for name in ASSETS
        ] + [
            f"app/static/{name}" for name in ASSETS
        ] + [
            f"desktop/ui/{name}" for name in ASSETS
        ]
        result = subprocess.run(
            ["git", "check-attr", "text", "eol", "--", *expected_paths],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stdout.strip(), "expected Git attributes for frontend asset paths")
        attrs = {}
        for line in result.stdout.splitlines():
            path, attr, value = line.split(": ", 2)
            attrs.setdefault(path, {})[attr] = value
        for repo_path in expected_paths:
            self.assertEqual(attrs[repo_path], {"text": "set", "eol": "lf"})

        with tempfile.TemporaryDirectory() as td:
            app_out, desktop_out, _ = self.run_temp_build(Path(td))
            generated = {
                f"app/static/{name}": (app_out / name).read_bytes() for name in ASSETS
            } | {
                f"desktop/ui/{name}": (desktop_out / name).read_bytes() for name in ASSETS
            }
            archive_blobs = git_archive_blob_bytes(list(generated))
            for repo_path, generated_bytes in generated.items():
                committed_bytes = git_blob_bytes(repo_path)
                self.assertEqual(
                    archive_blobs[repo_path],
                    committed_bytes,
                    f"git archive HEAD bytes for {repo_path} differ from committed HEAD blob",
                )
                self.assertEqual(
                    committed_bytes,
                    generated_bytes,
                    f"committed HEAD blob for {repo_path} differs from frontend/build.mjs output",
                )

    def test_git_blob_bytes_reads_committed_head_not_staged_index(self):
        committed_bytes = b"committed asset bytes\n"
        staged_bytes = b"staged-only asset bytes\n"
        repo_path = "app/static/app.js"
        old_root = ROOT
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            asset = repo / repo_path
            asset.parent.mkdir(parents=True)
            asset.write_bytes(committed_bytes)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
            subprocess.run(["git", "add", repo_path], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Frontend Contract Test",
                    "-c",
                    "user.email=frontend-contract@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "seed asset",
                ],
                cwd=repo,
                check=True,
            )
            asset.write_bytes(staged_bytes)
            subprocess.run(["git", "add", repo_path], cwd=repo, check=True)
            try:
                globals()["ROOT"] = repo
                blob = git_blob_bytes(repo_path)
                archive_blob = git_archive_blob_bytes([repo_path])[repo_path]
            finally:
                globals()["ROOT"] = old_root

        self.assertEqual(blob, committed_bytes)
        self.assertEqual(archive_blob, committed_bytes)
        self.assertNotEqual(blob, staged_bytes)
        self.assertNotEqual(archive_blob, staged_bytes)

    def test_build_is_deterministic_across_clean_output_directories(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_app, first_desktop, _ = self.run_temp_build(Path(first))
            second_app, second_desktop, _ = self.run_temp_build(Path(second))
            first_hashes = {
                f"app/static/{name}": sha256(first_app / name) for name in ASSETS
            } | {
                f"desktop/ui/{name}": sha256(first_desktop / name) for name in ASSETS
            }
            second_hashes = {
                f"app/static/{name}": sha256(second_app / name) for name in ASSETS
            } | {
                f"desktop/ui/{name}": sha256(second_desktop / name) for name in ASSETS
            }
            self.assertEqual(first_hashes, second_hashes)

    def test_build_replaces_only_declared_asset_triple(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sentinels = [
                root / "app" / "static" / "runtime-sentinel.json",
                root / "desktop" / "ui" / "runtime-sentinel.json",
            ]
            for sentinel in sentinels:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_text('{"keep":true}', encoding="utf-8")
            app_out, desktop_out, _ = self.run_temp_build(root)
            for sentinel in sentinels:
                self.assertTrue(sentinel.is_file(), f"build removed unrelated output file {sentinel}")
                self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"keep":true}')
            for output in (app_out, desktop_out):
                self.assertEqual(sorted(p.name for p in output.iterdir()), [
                    "app.js",
                    "index.html",
                    "runtime-sentinel.json",
                    "styles.css",
                ])

    def test_installed_qa_validate_only_runs_under_windows_powershell_51_from_ascii_source(self):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows PowerShell is not available on this host")
        runner = ROOT / "qa" / "run-installed-qa.ps1"
        script_bytes = runner.read_bytes()
        try:
            script_bytes.decode("ascii")
        except UnicodeDecodeError as error:
            self.fail(f"run-installed-qa.ps1 must be ASCII-source safe for Windows PowerShell 5.1 UTF-8-without-BOM parsing: {error}")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(runner),
                "-ValidateOnly",
                "-IsolatedUi",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"VALID installed-runner mode=isolated-ui endpoints=9 samples=5")

    def test_emitted_assets_are_csp_safe(self):
        html = read_text(APP_STATIC / "index.html")
        js = read_text(APP_STATIC / "app.js")
        css = read_text(APP_STATIC / "styles.css")
        self.assertNoInlineCspHazards(html, js, css)
        self.assertEqual(html, read_text(DESKTOP_UI / "index.html"))
        self.assertEqual(js, read_text(DESKTOP_UI / "app.js"))
        self.assertEqual(css, read_text(DESKTOP_UI / "styles.css"))

    def test_tauri_connection_and_bearer_transport_contract(self):
        js = read_text(APP_STATIC / "app.js")
        source = read_text(FRONTEND / "app.js")
        self.assertIn("from '@tauri-apps/api/core'", source)
        self.assertRegex(js, r"invoke\([\"']backend_connection[\"']\)")
        for command in ["auth_begin", "auth_status", "auth_cancel", "auth_logout"]:
            self.assertRegex(js, rf"invoke\([\"']{command}[\"']")
        self.assertNotRegex(js, r"invoke\([\"']service_login[\"']")
        self.assertNotIn("auth_open_setup", js)
        self.assertIn("Authorization", js)
        self.assertIn("Bearer ${connection.token}", js)
        self.assertNotIn("window.__TAURI__", js)
        self.assertNotRegex(js, r"localStorage|sessionStorage|indexedDB|document\.cookie")
        self.assertNotRegex(js, r"console\.(?:log|info|warn|error)\([^)]*token", re.IGNORECASE)
        self.assertNotRegex(js, r"[?&#]token=|[?&#][^'\"`\s]*\$\{connection\.token\}", "token must not be placed in URLs")
        self.assertRegex(js, r"function\s+isValidLoopbackBaseUrl")

    def test_non_packaged_transport_requires_explicit_validated_dev_connection(self):
        result = run_frontend_transport_probe(
            r"""
globalThis.__invokeCalls = [];
globalThis.__invokeResult = {baseUrl: 'http://127.0.0.1:7100', token: 'packaged-token'};
globalThis.window = {location: {protocol: 'http:', origin: 'http://localhost:5173'}};
const outcomes = [];
let fetchCalls = [];
globalThis.fetch = async (url, request) => {
  fetchCalls.push({url, request});
  return {ok: true, json: async () => ({ok: true})};
};

async function attempt(name, setup) {
  cachedConnection = null;
  fetchCalls = [];
  delete globalThis.__DECKPIPE_DEV_CONNECTION__;
  if (setup) setup();
  try {
    await api('/api/config');
    outcomes.push({name, ok: true, fetches: fetchCalls.length, auth: fetchCalls[0]?.request?.headers?.Authorization || null});
  } catch (error) {
    outcomes.push({name, ok: false, message: error.message, fetches: fetchCalls.length});
  }
}

await attempt('missing-dev-connection');
await attempt('empty-token', () => { globalThis.__DECKPIPE_DEV_CONNECTION__ = {baseUrl: 'http://127.0.0.1:7100', token: ''}; });
await attempt('non-loopback', () => { globalThis.__DECKPIPE_DEV_CONNECTION__ = {baseUrl: 'https://example.test', token: 'dev-token'}; });
await attempt('valid', () => { globalThis.__DECKPIPE_DEV_CONNECTION__ = {baseUrl: 'http://127.0.0.1:7100', token: 'dev-token'}; });
console.log(JSON.stringify(outcomes));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        outcomes = json.loads(result.stdout)
        self.assertEqual(outcomes[:3], [
            {"name": "missing-dev-connection", "ok": False, "message": "backend connection unavailable", "fetches": 0},
            {"name": "empty-token", "ok": False, "message": "backend connection unavailable", "fetches": 0},
            {"name": "non-loopback", "ok": False, "message": "backend connection unavailable", "fetches": 0},
        ])
        self.assertEqual(outcomes[3], {
            "name": "valid",
            "ok": True,
            "fetches": 1,
            "auth": "Bearer dev-token",
        })

    def test_one_fetch_call_inside_api_transport(self):
        js = read_text(APP_STATIC / "app.js")
        self.assertEqual(len(re.findall(r"\bfetch\s*\(", js)), 1)
        fetch_index = js.index("fetch(")
        wrapper_start = js.rfind("async function api", 0, fetch_index)
        self.assertNotEqual(wrapper_start, -1, "fetch must live inside api transport wrapper")

    def test_all_data_actions_are_created_by_real_dom_helpers_and_represented_once(self):
        source = read_text(FRONTEND / "app.js")
        html = read_text(FRONTEND / "index.html")
        static_actions = sorted(set(re.findall(r"data-action=\"([a-z0-9-]+)\"", html)))
        result = run_frontend_action_probe(
            "const documentHtmlActions = new Set(" + json.dumps(static_actions) + ");\n" + r"""
globalThis.fetch = async url => {
  const path = new URL(url).pathname;
  const fixtures = {
    '/api/errors': [{playlist_key: 'p1', playlist_title: 'Playlist', track: {title: 'Broken', artist: 'Artist'}, error: 'failed'}],
    '/api/remote-actions': [{id: 'remote-1', state: 'failed', target_id: 'p1', track_ids: ['t1'], last_error: {message: 'retry'}}],
    '/api/playlists': [{id: 'p1', title: 'Playlist', count: 1}],
    '/api/sc/sources': [{id: 's1', title: 'Source', count: 1}],
    '/api/local/playlists': [{key: 'local:one', title: 'Local', count: 1}],
  };
  return {ok: true, json: async () => fixtures[path] || []};
};
const emitted = new Set();
for (const action of Array.from(documentHtmlActions)) emitted.add(action);
for (const node of [
  playlistControl('Playlist', create('img', {attrs: {alt: ''}}), [create('span', {text: 'Playlist'})], 'select-playlist', {id: 'p1', title: 'Playlist'}, false),
  playlistControl('Source', create('img', {attrs: {alt: ''}}), [create('span', {text: 'Source'})], 'select-sc-source', {id: 's1', title: 'Source'}, false),
  playlistControl('Target', create('img', {attrs: {alt: ''}}), [create('span', {text: 'Target'})], 'set-search-target', {index: 0}, false),
  trackRow('deezer', 0, {id: 't1', title: 'Track', artist: 'Artist', duration: 10}),
  ...albumRow('deezer', 0, {id: 'a1', title: 'Album', artist: 'Artist', count: 2}),
]) {
  collectActions(node).forEach(action => emitted.add(action));
}
searchExpanded = {'deezer:0': {tracks: [{id: 'a1t1', title: 'Album Track', artist: 'Artist', duration: 20}]}};
albumRow('deezer', 0, {id: 'a1', title: 'Album', artist: 'Artist', count: 2}).forEach(node =>
  collectActions(node).forEach(action => emitted.add(action)));
searchTarget = {key: 'target', title: 'Target', provider: 'deezer'};
searchSel = {'deezer:0': {id: 't1', title: 'Track', artist: 'Artist', duration: 10, provider: 'deezer'}};
_renderBasket();
collectActions(elements.get('#basket')).forEach(action => emitted.add(action));
tracks = [
  {id: 't1', title: 'Track', artist: 'Artist', album: 'Album', duration: 10, status: 'missing'},
  {id: 't2', title: 'Ambiguous', artist: 'Artist', album: 'Album', duration: 10, status: 'ambiguous', locations: [{path: 'C:/Music/Artist - Ambiguous.flac'}]},
];
renderTracks();
collectActions(elements.get('#tracks')).forEach(action => emitted.add(action));
window._errors = [{playlist_key: 'p1', playlist_title: 'Playlist', track: {title: 'Broken', artist: 'Artist'}, error: 'failed'}];
await loadErrors();
collectActions(elements.get('#playlists')).forEach(action => emitted.add(action));
libraryConfigured = true;
await loadSearchTargets();
collectActions(elements.get('#playlists')).forEach(action => emitted.add(action));
await loadLocalPlaylists();
collectActions(elements.get('#playlists')).forEach(action => emitted.add(action));
current = {kind: 'local', id: 'local:one', title: 'Local'};
const rbOperation = beginRbOperation();
const rbDialog = chooseRbTargetDialog(rbOperation, 'Local', [
  {id: 'rb-1', name: 'Local', count: 1},
  {id: 'rb-2', name: 'Local', count: 2},
]);
for (const selector of ['#btnAuthStart', '#btnAuthRetry']) {
  collectActions(elements.get(selector)).forEach(action => emitted.add(action));
}
cancelRbDialog();
await rbDialog;
finishRbOperation(rbOperation);
window.setTimeout=()=>1; window.clearTimeout=()=>{};
renderJobs([{id:'job-1',title:'Download',mode:'append',state:'running',done:0,total:1}]);
collectActions(elements.get('#jobs')).forEach(action => emitted.add(action));
showStatus('Completed');
collectActions(elements.get('#statusRegion')).forEach(action => emitted.add(action));
logsBefore='10'; renderLogs();
collectActions(elements.get('#tracks')).forEach(action => emitted.add(action));
console.log(JSON.stringify({
  emitted: [...emitted].sort(),
  click: Object.keys(clickActions).sort(),
  change: Object.keys(changeActions).sort(),
}));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)
        emitted = set(payload["emitted"])
        click_keys = set(payload["click"])
        change_keys = set(payload["change"])
        self.assertNotIn("ACTION_MARKUP_CONTRACT", source)
        self.assertTrue(emitted, "expected canonical UI actions")
        self.assertFalse(click_keys & change_keys, f"action keys must belong to exactly one handler map: {click_keys & change_keys}")
        self.assertEqual(emitted, click_keys | change_keys)

    def test_external_values_are_not_raw_in_generated_attributes_or_classes(self):
        source = read_text(FRONTEND / "app.js")
        raw_attribute_patterns = {
            r'data-id="\$\{p\.id\}"': "SoundCloud account ids must be attribute-escaped",
            r'data-id="\$\{s\.id\}"': "SoundCloud source ids must be attribute-escaped",
            r'data-title="\$\{esc\(': "data-title values must use the attribute escaping boundary",
            r'title="\$\{esc\(': "title attributes must use the attribute escaping boundary",
            r'data-url="\$\{esc\(': "URL data attributes must use the attribute escaping boundary",
            r'<img src="\$\{p\.cover\}"': "cover image URLs must be constrained before interpolation",
            r'class="fmt\s+\$\{t\.format\}"': "format class tokens must come from a fixed mapping",
            r'>\$\{t\.format\}</span>': "format label text must be escaped",
        }
        for pattern, message in raw_attribute_patterns.items():
            self.assertNotRegex(source, pattern, message)
        self.assertRegex(source, r"function\s+safeCoverUrl")
        self.assertRegex(source, r"url\.protocol\s*===\s*['\"]https:['\"]")
        self.assertRegex(source, r"FORMAT_CLASS_BY_VALUE\s*=\s*Object\.freeze")

    def test_required_labels_and_routes_remain_represented(self):
        combined = read_text(APP_STATIC / "index.html") + "\n" + read_text(APP_STATIC / "app.js")
        combined_bytes = (APP_STATIC / "index.html").read_bytes() + (APP_STATIC / "app.js").read_bytes()
        for label_hex in [
            "4465657a65723a20d0b2d185d0bed0b4",
            "53433a20d0b2d185d0bed0b4",
            "d09fd0bed0b8d181d0ba",
            "d09ed188d0b8d0b1d0bad0b8",
            "d0a1d0bed185d180d0b0d0bdd0b8d182d18c",
            "d091d0b0d0b3d180d0b5d0bfd0bed180d182",
            "d09fd0b5d180d0b5d181d0bad0b0d0bdd0b8d180d0bed0b2d0b0d182d18c",
            "d0a1d0bad0b0d187d0b0d182d18c20d0b2d18bd0b1d180d0b0d0bdd0bdd18bd0b5",
            "e2878420574156",
            "d09fd0bed0b2d182d0bed180d0b8d182d18c",
            "d098d0bcd0bfd0bed180d182d0b8d180d0bed0b2d0b0d182d18c20d0b2d18bd0b1d180d0b0d0bdd0bdd18bd0b5",
        ]:
            self.assertIn(bytes.fromhex(label_hex), combined_bytes)
        for route in [
            "/api/config",
            "/api/library/status",
            "/api/library/scan",
            "/api/library/confirm",
            "/api/sc/account",
            "/api/sc/account/import",
            "/api/report",
            "/api/playlists",
            "/api/errors",
            "/api/errors/retry",
            "/api/remote-actions",
            "/api/search",
            "/api/search/download",
            "/api/sc/sources",
            "/api/sc/sync-account",
            "/api/deezer/playlist/create",
            "/api/deezer/playlist/add",
            "/api/local/playlists",
            "/api/playlists/",
            "/api/rb/sync",
            "/api/flip",
            "/api/browse",
            "/api/jobs",
        ]:
            self.assertIn(route, combined)
        self.assertNotIn("/api/login/deezer", combined)
        self.assertNotIn("/api/login/soundcloud", combined)
        self.assertNotIn("/api/login/deezer/password", combined)
        self.assertNotIn("loginPassword", combined)
        self.assertNotIn("loginEmail", combined)
        self.assertNotRegex(combined, r"type=[\"']password[\"']")

    def test_node_syntax_checks_source_and_generated_scripts(self):
        for script in [
            FRONTEND / "app.js",
            FRONTEND / "build.mjs",
            APP_STATIC / "app.js",
            DESKTOP_UI / "app.js",
        ]:
            result = subprocess.run(
                ["node", "--check", str(script)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(result.returncode, 0, f"{script.relative_to(ROOT)}\n{result.stdout}")


if __name__ == "__main__":
    unittest.main()
