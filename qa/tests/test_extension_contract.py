from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "extension"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class ExtensionContractTests(unittest.TestCase):
    maxDiff = None

    def test_manifest_limits_native_auth_to_selected_provider_origins(self) -> None:
        manifest = json.loads(read_text(EXTENSION / "manifest.json"))

        self.assertEqual(3, manifest["manifest_version"])
        self.assertEqual({"nativeMessaging", "cookies"}, set(manifest["permissions"]))
        self.assertEqual(
            {
                "https://www.deezer.com/*",
                "https://deezer.com/*",
                "https://soundcloud.com/*",
            },
            set(manifest["optional_host_permissions"]),
        )
        self.assertNotIn("host_permissions", manifest)
        self.assertNotIn("content_scripts", manifest)
        self.assertNotIn("externally_connectable", manifest)
        self.assertEqual("not_allowed", manifest["incognito"])
        self.assertEqual(["helper-core.js", "background.js"], manifest["background"]["scripts"])
        self.assertEqual({"default_popup", "default_title"}, set(manifest["action"]))

        gecko = manifest["browser_specific_settings"]["gecko"]
        self.assertEqual("deckpipe-auth@deckpipe.local", gecko["id"])
        self.assertEqual(["authenticationInfo"], gecko["data_collection_permissions"]["required"])

    def test_helper_behavior_rejects_foreign_tabs_and_redacts_credentials(self) -> None:
        result = subprocess.run(
            ["node", "--test", str(ROOT / "qa" / "tests" / "auth_helper.test.cjs")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("tests 4", result.stdout)
        self.assertIn("pass 4", result.stdout)
        self.assertIn("fail 0", result.stdout)

    def test_background_accepts_only_popup_messages_and_exact_native_host(self) -> None:
        source = read_text(EXTENSION / "background.js")

        self.assertIn("connectNative('com.deckpipe.auth')", source)
        self.assertIn("sender.id!==browser.runtime.id", source)
        self.assertIn("sender.url!==browser.runtime.getURL('popup.html')", source)
        self.assertIn("Object.keys(message).length!==1", source)
        self.assertRegex(source, r"\^\[a-f0-9\]\{64\}\$")
        self.assertIn("browser.permissions.contains", source)
        self.assertIn("credential=null", source)
        self.assertIn("disconnect()", source)
        for forbidden in ["onMessageExternal", "externally_connectable", "tabs.sendMessage", "window.postMessage"]:
            self.assertNotIn(forbidden, source)

    def test_popup_explains_user_owned_login_without_rendering_secrets(self) -> None:
        popup_html = read_text(EXTENSION / "popup.html")
        popup_js = read_text(EXTENSION / "popup.js")
        combined = f"{popup_html}\n{popup_js}"
        lower = combined.lower()

        self.assertIn('lang="ru"', popup_html)
        self.assertIn("Откройте страницу нужного сервиса в этом профиле Firefox", combined)
        self.assertIn("браузер", combined.lower())
        self.assertIn("browser.permissions.request", popup_js)
        self.assertIn("browser.runtime.sendMessage({op:'connect'})", popup_js)
        self.assertIn("DeckPipeHelper.publicReply", popup_js)
        for forbidden in [
            "type=\"password\"",
            "type='password'",
            "clipboard",
            "fetch(",
            "xmlhttprequest",
            "127.0.0.1",
            "localhost",
            "/api/login",
            "oauth_token",
            "credential",
            "synthetic-secret",
        ]:
            self.assertNotIn(forbidden, lower)


if __name__ == "__main__":
    unittest.main(verbosity=2)
