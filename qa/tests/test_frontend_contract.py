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
        for selector in ["#tab-deezer", "#tab-sc", "#tab-search", "#tab-errors", "#musicRoot", "#wavMode"]:
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


if __name__ == "__main__":
    unittest.main()
