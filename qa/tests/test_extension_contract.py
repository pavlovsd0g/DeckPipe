from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "extension"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ExtensionContractTests(unittest.TestCase):
    maxDiff = None

    def test_manifest_exposes_only_static_popup_without_cookie_or_bridge_permissions(self) -> None:
        manifest = json.loads(read_text(EXTENSION / "manifest.json"))

        self.assertEqual(3, manifest["manifest_version"])
        self.assertEqual([], manifest.get("permissions", []))
        self.assertNotIn("host_permissions", manifest)
        self.assertNotIn("background", manifest)
        self.assertNotIn("content_scripts", manifest)
        self.assertNotIn("externally_connectable", manifest)
        self.assertEqual({"default_popup", "default_title"}, set(manifest["action"]))

        raw = json.dumps(manifest, ensure_ascii=False).lower()
        for forbidden in [
            "cookies",
            "native",
            "127.0.0.1",
            "localhost",
            "deezer.com",
            "soundcloud.com",
            "background.js",
            "service_worker",
            "content_scripts",
        ]:
            self.assertNotIn(forbidden, raw)

    def test_popup_is_russian_visible_deprecation_copy_with_no_interactive_bridge(self) -> None:
        popup_html = read_text(EXTENSION / "popup.html")
        popup_js = read_text(EXTENSION / "popup.js")
        combined = f"{popup_html}\n{popup_js}".lower()

        self.assertIn('lang="ru"', popup_html)
        self.assertIn("расширение больше не передает вход", combined)
        self.assertIn("используйте вход в deckpipe desktop", combined)
        self.assertIn("deezer", combined)
        self.assertIn("soundcloud", combined)
        self.assertNotIn("desktop login", combined)

        for forbidden in [
            "chrome.",
            "browser.",
            "cookies",
            "fetch(",
            "xmlhttprequest",
            "abortsignal",
            "127.0.0.1",
            "localhost",
            "/api/login/from-browser",
            "oauth_token",
            "arl",
            "port",
            "24680",
            "7100",
            "native",
            "clipboard",
            "location.href",
            "window.open",
            "addEventListener",
        ]:
            self.assertNotIn(forbidden, combined)

    def test_background_worker_is_removed(self) -> None:
        self.assertFalse((EXTENSION / "background.js").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
