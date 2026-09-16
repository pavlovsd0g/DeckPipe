import re
import subprocess
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


def run_frontend_probe(probe_script):
    source = read_text(FRONTEND / "app.js")
    transport_only = source.split("async function loadConfig", 1)[0]
    transport_only = re.sub(
        r"import\s+\{\s*invoke\s*\}\s+from\s+['\"]@tauri-apps/api/core['\"]\s*;",
        "const invoke = async name => { globalThis.__invokeCalls.push(name); return globalThis.__invokeResult; };",
        transport_only,
        count=1,
    )
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "frontend-contract-probe.mjs"
        probe.write_text(transport_only + "\n" + probe_script, encoding="utf-8")
        return subprocess.run(
            ["node", str(probe)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


def run_frontend_app_probe(probe_script):
    source = read_text(FRONTEND / "app.js")
    source = re.sub(
        r"import\s+\{\s*invoke\s*\}\s+from\s+['\"]@tauri-apps/api/core['\"]\s*;",
        "const invoke = async (...args) => { if (globalThis.__invokeImpl) return globalThis.__invokeImpl(...args); globalThis.__invokeCalls.push(args[0]); return globalThis.__invokeResult; };",
        source,
        count=1,
    )
    source = source.replace("\ninit();\n", "\n// init skipped by frontend behavior probe\n")
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
  focus() { globalThis.document.activeElement = this; }
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
globalThis.__invokeCalls = [];
globalThis.__invokeResult = {baseUrl: 'http://127.0.0.1:24680', token: 'packaged-token'};
globalThis.window = {location: {protocol: 'tauri:', origin: 'http://tauri.localhost'},
  setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {}};
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
  if (node.dataset && node.dataset.action) {
    out.push({
      action: node.dataset.action,
      label: node.textContent,
      aria: node.getAttribute ? node.getAttribute('aria-label') : null,
      title: node.title || '',
    });
  }
  for (const child of node.children || []) collectActions(child, out);
  return out;
}
async function driveRbDialog({confirm = true, targetId = null} = {}) {
  for (let index = 0; index < 40 && !(typeof pendingRbDialog !== 'undefined' && pendingRbDialog); index++) await Promise.resolve();
  if (!(typeof pendingRbDialog !== 'undefined' && pendingRbDialog)) return false;
  if (targetId !== null) elements.get('#rbTargetSelect').value = String(targetId);
  if (confirm) confirmRbDialog(); else cancelRbDialog();
  return true;
}
"""
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "frontend-app-probe.mjs"
        probe.write_text(prelude + "\n" + source + "\n" + probe_script, encoding="utf-8")
        return subprocess.run(
            ["node", str(probe)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


class FrontendInteractionContractTests(unittest.TestCase):
    maxDiff = None

    def test_no_inline_handlers_or_unsafe_html_sinks(self):
        html = read_text(FRONTEND / "index.html")
        js = read_text(FRONTEND / "app.js")
        css = read_text(FRONTEND / "styles.css")
        self.assertNotRegex(html, r"\son[a-z]+\s*=", "static HTML must not use inline event handlers")
        self.assertNotRegex(html, r"javascript\s*:", "static HTML must not use javascript: URLs")
        self.assertNotRegex(js, r"\.(?:innerHTML|outerHTML)\s*=", "API/user values must never be rendered through HTML string sinks")
        self.assertNotRegex(js, r"insertAdjacentHTML\s*\(", "dynamic rendering must use nodes, not HTML parsing")
        self.assertNotRegex(js, r"new\s+DOMParser\s*\(", "dynamic rendering must not parse HTML strings")
        self.assertNotRegex(js, r"document\.write\s*\(", "dynamic rendering must not write parsed HTML")
        self.assertRegex(js, r"\bcreateTextNode\s*\(", "dynamic text rendering must use text nodes")
        self.assertRegex(js, r"\btextContent\s*=", "status/control text must be assigned as text")
        self.assertNotRegex(css, r"outline\s*:\s*(?:0|none)\b", "focus styling must not globally suppress outlines")

    def test_generated_assets_match_canonical_after_contract_changes(self):
        with tempfile.TemporaryDirectory() as td:
            app_out = Path(td) / "app" / "static"
            desktop_out = Path(td) / "desktop" / "ui"
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
            for name in ASSETS:
                self.assertEqual((app_out / name).read_bytes(), (APP_STATIC / name).read_bytes(), f"app/static/{name} drifted")
                self.assertEqual((desktop_out / name).read_bytes(), (DESKTOP_UI / name).read_bytes(), f"desktop/ui/{name} drifted")
                self.assertEqual((app_out / name).read_bytes(), (desktop_out / name).read_bytes(), f"generated targets differ for {name}")

    def test_keyboard_access_contract_is_explicit(self):
        html = read_text(FRONTEND / "index.html")
        js = read_text(FRONTEND / "app.js")
        css = read_text(FRONTEND / "styles.css")
        self.assertNotRegex(html, r"<span\b[^>]*\bdata-action=", "action chips must be real controls")
        self.assertNotRegex(js, r"createElement\(['\"](?:div|span)['\"]\)[\s\S]{0,240}?dataset\.action", "dynamic actions must be native controls or explicitly keyboard-enabled")
        self.assertRegex(js, r"document\.addEventListener\(['\"]keydown['\"]", "keyboard activation and Escape handling must be delegated")
        self.assertRegex(js, r"\bactivateAction\b", "Enter/Space keyboard activation must share the delegated action path")
        self.assertRegex(css, r":focus-visible", "keyboard focus must be visibly styled")
        for selector in ["#tab-deezer", "#tab-sc", "#tab-local", "#tab-search", "#tab-errors", "#musicRoot", "#wavMode"]:
            self.assertIn(selector[1:], html)

    def test_dialog_focus_trap_escape_and_restore_are_implemented(self):
        html = read_text(FRONTEND / "index.html")
        js = read_text(FRONTEND / "app.js")
        self.assertRegex(html, r"id=\"modal\"[^>]*role=\"dialog\"", "login modal must expose dialog semantics")
        self.assertRegex(html, r"id=\"modal\"[^>]*aria-modal=\"true\"", "login modal must be modal for assistive tech")
        self.assertRegex(html, r"id=\"modal\"[^>]*aria-labelledby=\"loginTitle\"", "dialog must have a semantic name")
        for symbol in ["trapDialogFocus", "releaseDialogFocus", "previousFocus", "focusableDialogElements"]:
            self.assertIn(symbol, js)
        self.assertRegex(js, r"event\.key\s*===\s*['\"]Escape['\"]", "Escape must close the dialog when safe")
        self.assertRegex(js, r"event\.key\s*===\s*['\"]Tab['\"]", "Tab must be trapped inside the open dialog")
        self.assertRegex(js, r"previousFocus\.focus\s*\(", "closing the dialog must restore focus")

    def test_live_regions_and_truthful_error_categories_are_present(self):
        html = read_text(FRONTEND / "index.html")
        js = read_text(FRONTEND / "app.js")
        self.assertRegex(html, r"id=\"statusRegion\"[^>]*(?:role=\"status\"|aria-live=\"polite\")")
        self.assertRegex(html, r"id=\"errorRegion\"[^>]*(?:role=\"alert\"|aria-live=\"assertive\")")
        for phrase in [
            "Локальная служба",
            "Защита API",
            "Состояние библиотеки",
            "Rekordbox",
            "Dry-run",
            "Apply",
            "Reconcile",
        ]:
            self.assertIn(phrase, js)
        self.assertRegex(js, r"function\s+describeError", "errors must be normalized before display")
        self.assertRegex(js, r"function\s+showError", "errors must flow into an accessible alert region")
        self.assertNotRegex(js, r"\balert\s*\(", "errors/results must not rely on inaccessible alert dialogs")

    def test_tauri_login_cancel_is_suppressed_but_other_login_errors_surface(self):
        result = run_frontend_app_probe(
            r"""
globalThis.fetch = async () => {
  throw new Error('login cancel test must not call backend after invoke failure');
};
const outcomes = [];
for (const [name, rejection] of [
  ['cancel', 'DECKPIPE_LOGIN_CANCELLED'],
  ['other', 'login window unavailable'],
]) {
  elements.get('#errorRegion').textContent = '';
  elements.get('#errorRegion').classList.add('hidden');
  elements.get('#statusRegion').textContent = '';
  globalThis.__invokeImpl = async () => { throw rejection; };
  await tauriLogin('deezer');
  outcomes.push({
    name,
    error: elements.get('#errorRegion').textContent,
    hidden: elements.get('#errorRegion').classList.contains('hidden'),
    status: elements.get('#statusRegion').textContent,
  });
}
console.log(JSON.stringify(outcomes));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        outcomes = __import__("json").loads(result.stdout)
        self.assertEqual({"name": "cancel", "error": "", "hidden": True, "status": ""}, outcomes[0])
        self.assertEqual("other", outcomes[1]["name"])
        self.assertFalse(outcomes[1]["hidden"])
        self.assertIn("login window unavailable", outcomes[1]["error"])

    def test_minimum_size_and_reduced_motion_contracts_are_encoded(self):
        html = read_text(FRONTEND / "index.html")
        css = read_text(FRONTEND / "styles.css")
        self.assertIn('data-min-width="1000"', html)
        self.assertIn('data-min-height="640"', html)
        self.assertRegex(css, r"@media\s*\([^)]*max-width\s*:\s*1000px", "minimum supported width needs an explicit responsive layout")
        self.assertRegex(css, r"@media\s*\([^)]*prefers-reduced-motion\s*:\s*reduce", "reduced-motion users must be respected")
        self.assertRegex(css, r"overflow-x\s*:\s*auto", "wide data surfaces must scroll instead of clipping critical controls")
        self.assertRegex(css, r"min-height\s*:\s*0", "nested scroll regions must be allowed to shrink at 1000x640")

    def test_connection_wrapper_keeps_launch_token_in_memory_only(self):
        result = run_frontend_probe(
            r"""
globalThis.__invokeCalls = [];
globalThis.__invokeResult = {baseUrl: 'http://127.0.0.1:24680', token: 'packaged-token'};
globalThis.window = {location: {protocol: 'tauri:', origin: 'http://tauri.localhost'}};
const writes = [];
globalThis.localStorage = {setItem: (...args) => writes.push(['localStorage', args])};
globalThis.sessionStorage = {setItem: (...args) => writes.push(['sessionStorage', args])};
globalThis.indexedDB = {open: (...args) => writes.push(['indexedDB', args])};
globalThis.document = {cookie: ''};
const first = await getConnection();
const second = await getConnection();
console.log(JSON.stringify({first, same: first === second, invokeCalls: globalThis.__invokeCalls, writes, cookie: globalThis.document.cookie}));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('"same":true', result.stdout)
        self.assertIn('"invokeCalls":["backend_connection"]', result.stdout)
        self.assertIn('"writes":[]', result.stdout)
        self.assertIn('"cookie":""', result.stdout)

    def test_rekordbox_sync_defaults_to_truthful_dry_run_without_legacy_fields(self):
        result = run_frontend_app_probe(
            r"""
const fetchCalls = [];
globalThis.fetch = async (url, request) => {
  fetchCalls.push({url, body: JSON.parse(request.body)});
  return {ok: true, json: async () => ({
    dry_run: true,
    applied: false,
    reconciled: false,
    unresolved: [{code: 'missing_desired_path'}],
    backup_id: null,
    error: null,
    plan: {
      hash: 'a'.repeat(64),
      counts: {desired: 9, current: 8, resolved: 7, add: 2, remove: 1, reorder: 3, metadata: 4, path: 5, unresolved: 6},
    },
  })};
};
const confirms = [];
globalThis.confirm = message => { confirms.push(message); return false; };
current = {kind: 'deezer', id: 'playlist-1', title: 'Set One'};
await rbSync();
const status = elements.get('#statusRegion').textContent;
console.log(JSON.stringify({
  fetchCalls,
  confirms,
  status,
  dryRunSaysNoMutation: status.includes('изменения не применялись'),
  dryRunSaysNoBackup: status.includes('бэкап не создавался'),
  error: elements.get('#errorRegion').textContent,
}));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = __import__("json").loads(result.stdout)
        self.assertEqual(len(payload["fetchCalls"]), 1)
        self.assertRegex(payload["fetchCalls"][0]["url"], r"/api/rb/sync\?dry_run=true$")
        self.assertEqual(payload["fetchCalls"][0]["body"], {"playlist_key": "playlist-1", "playlist_title": "Set One"})
        self.assertIn("не разрешены", payload["error"].lower())
        self.assertEqual(payload["status"], "")
        self.assertIn("Изменения не применялись", payload["error"])
        self.assertNotIn("undefined", payload["status"])
        self.assertNotRegex(payload["status"], r"added_content|added_to_playlist|result\.playlist|note")

    def test_rekordbox_apply_sends_internal_confirmation_with_viewed_hash_and_reports_backup(self):
        result = run_frontend_app_probe(
            r"""
const responses = [
  {
    dry_run: true,
    applied: false,
    reconciled: false,
    unresolved: [],
    backup_id: null,
    error: null,
    plan: {hash: 'b'.repeat(64), counts: {desired: 2, current: 1, resolved: 2, add: 1, remove: 0, reorder: 1, metadata: 0, path: 0, unresolved: 0}},
  },
  {
    dry_run: false,
    applied: true,
    reconciled: true,
    unresolved: [],
    backup_id: 'backup-123',
    error: null,
    plan: {hash: 'c'.repeat(64), counts: {desired: 2, current: 1, resolved: 2, add: 1, remove: 0, reorder: 1, metadata: 0, path: 0, unresolved: 0}},
  },
];
const fetchCalls = [];
globalThis.fetch = async (url, request) => {
  fetchCalls.push({url, body: JSON.parse(request.body)});
  return {ok: true, json: async () => responses.shift()};
};
globalThis.confirm = () => { throw new Error('native confirm must not be shown'); };
const prompts = [];
globalThis.prompt = message => { prompts.push(message); throw new Error('technical token prompt must not be shown'); };
current = {kind: 'sc', id: 'source-7', title: 'SC Set'};
const operation = rbSync();
await driveRbDialog();
await operation;
console.log(JSON.stringify({fetchCalls, prompts, status: elements.get('#statusRegion').textContent}));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = __import__("json").loads(result.stdout)
        self.assertEqual(len(payload["fetchCalls"]), 2)
        self.assertRegex(payload["fetchCalls"][0]["url"], r"/api/rb/sync\?dry_run=true$")
        self.assertRegex(payload["fetchCalls"][1]["url"], r"/api/rb/sync\?dry_run=false&confirmation_token=APPLY_REKORDBOX_CHANGES$")
        self.assertEqual(payload["fetchCalls"][0]["body"], {"playlist_key": "sc:source-7", "playlist_title": "SC Set"})
        self.assertEqual(payload["fetchCalls"][1]["body"], {
            "playlist_key": "sc:source-7",
            "playlist_title": "SC Set",
            "expected_plan_hash": "b" * 64,
        })
        self.assertEqual(payload["prompts"], [])
        self.assertIn("изменения применены", payload["status"].lower())
        self.assertIn("применены и проверены", payload["status"].lower())
        self.assertIn("Резервная копия создана и проверена", payload["status"])
        self.assertNotIn("backup-123", payload["status"])

    def test_dynamic_add_download_and_search_actions_have_accessible_names(self):
        result = run_frontend_app_probe(
            r"""
searchTarget = {key: 'dz1', title: 'Target', provider: 'deezer'};
searchSel = {'deezer:0': {id: 't1', title: 'Track', artist: 'Artist', duration: 120, provider: 'deezer'}};
const nodes = [
  trackRow('deezer', 0, {id: 't1', title: 'Track', artist: 'Artist', duration: 120}),
  ...albumRow('deezer', 0, {id: 'a1', title: 'Album', artist: 'Artist', count: 2}),
];
searchExpanded = {'deezer:0': {tracks: [{id: 'at1', title: 'Album Track', artist: 'Artist', duration: 90}]}};
nodes.push(...albumRow('deezer', 0, {id: 'a1', title: 'Album', artist: 'Artist', count: 2}));
_renderBasket();
nodes.push(elements.get('#basket'));
const actions = nodes.flatMap(node => collectActions(node)).filter(item =>
  ['add-dz-track', 'dl-search-track', 'toggle-album', 'dl-album-track', 'dl-whole-album', 'dl-basket', 'clear-basket', 'track-checkbox'].includes(item.action));
console.log(JSON.stringify(actions));
"""
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        actions = __import__("json").loads(result.stdout)
        self.assertTrue(actions, result.stdout)
        labels = {item["action"]: item for item in actions}
        for action in ["add-dz-track", "dl-search-track", "toggle-album", "dl-album-track", "dl-whole-album", "dl-basket", "clear-basket", "track-checkbox"]:
            self.assertIn(action, labels)
        for item in actions:
            self.assertTrue((item.get("aria") or item.get("label") or item.get("title") or "").strip(), item)


if __name__ == "__main__":
    unittest.main()
