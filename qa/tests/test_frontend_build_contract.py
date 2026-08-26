import hashlib
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


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        self.assertRegex(js, r"invoke\([\"']service_login[\"']")
        self.assertIn("Authorization", js)
        self.assertIn("Bearer ${connection.token}", js)
        self.assertNotIn("window.__TAURI__", js)
        self.assertNotRegex(js, r"localStorage|sessionStorage|indexedDB|document\.cookie")
        self.assertNotRegex(js, r"console\.(?:log|info|warn|error)\([^)]*token", re.IGNORECASE)
        self.assertNotRegex(js, r"[?&#]token=|[?&#][^'\"`\s]*\$\{connection\.token\}", "token must not be placed in URLs")
        self.assertRegex(js, r"function\s+isValidLoopbackBaseUrl")

    def test_one_fetch_call_inside_api_transport(self):
        js = read_text(APP_STATIC / "app.js")
        self.assertEqual(len(re.findall(r"\bfetch\s*\(", js)), 1)
        fetch_index = js.index("fetch(")
        wrapper_start = js.rfind("async function api", 0, fetch_index)
        self.assertNotEqual(wrapper_start, -1, "fetch must live inside api transport wrapper")

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
            "d0a1d0b8d0bdd0ba3a20d0bfd0be20d0bfd0bed180d18fd0b4d0bad183",
            "d0a1d0b8d0bdd0ba3a20d0bdd0bed0b2d18bd0b520d0b2d0bdd0b8d0b7",
            "e28692205242",
            "e2878420574156",
            "d09fd0bed0b2d182d0bed180d0b8d182d18c",
            "d098d0bcd0bfd0bed180d182d0b8d180d0bed0b2d0b0d182d18c20d0b2d18bd0b1d180d0b0d0bdd0bdd18bd0b5",
        ]:
            self.assertIn(bytes.fromhex(label_hex), combined_bytes)
        for route in [
            "/api/config",
            "/api/login/deezer",
            "/api/login/deezer/password",
            "/api/login/soundcloud",
            "/api/sc/account",
            "/api/sc/account/import",
            "/api/report",
            "/api/playlists",
            "/api/errors",
            "/api/errors/retry",
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
