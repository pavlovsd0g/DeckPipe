import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class EmbeddedAuthPackageTests(unittest.TestCase):
    def test_release_payload_contains_only_application_and_backend_auth_boundary(self):
        config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(["binaries/deckpipe-backend"], config["bundle"]["externalBin"])
        self.assertNotIn("resources", config["bundle"])

        build = (ROOT / "release/build.ps1").read_text(encoding="utf-8")
        for forbidden in ["auth-helper", "deckpipe-auth-host", "Build-AuthHelper"]:
            self.assertNotIn(forbidden, build)

    def test_obsolete_extension_and_helper_inventory_is_absent(self):
        tracked = subprocess.run(
            ["git", "ls-files", "extension", "release/auth-helper"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.splitlines()
        self.assertEqual([], tracked)

    def test_current_docs_describe_only_embedded_login(self):
        for document in [ROOT / "README.md", ROOT / "BUILD.md"]:
            text = document.read_text(encoding="utf-8")
            self.assertIn("DeckPipe", text)
            self.assertNotIn("Firefox", text)
            self.assertNotIn("auth-helper", text)
            self.assertNotIn("расширен", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
