import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "qa" / "run-installed-qa.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
BUILD_ID = "0.6.0+20260827.050713.6456dba254a6"


class InstalledRunnerContractTests(unittest.TestCase):
    maxDiff = None

    def run_runner(self, *args, env=None):
        command = [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            *map(str, args),
        ]
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            command,
            cwd=ROOT,
            env=merged_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )

    def make_candidate(self, directory: Path):
        exe = directory / "deckpipe-candidate.exe"
        exe.write_bytes(b"synthetic deckpipe candidate\n")
        digest = hashlib.sha256(exe.read_bytes()).hexdigest().upper()
        manifest = directory / "version.json"
        manifest.write_text(
            json.dumps({"version": "0.6.0", "build_id": BUILD_ID}) + "\n",
            encoding="utf-8",
        )
        return exe, digest, manifest

    def parse_selftest_json(self, result):
        for line in result.stdout.splitlines():
            if line.startswith("SELFTEST_JSON "):
                return json.loads(line[len("SELFTEST_JSON ") :])
        self.fail(f"SELFTEST_JSON line missing from output:\n{result.stdout}")

    def test_candidate_identity_failures_stop_before_launch(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            exe, digest, manifest = self.make_candidate(Path(tmp))
            cases = [
                (
                    "missing-sha",
                    [
                        "-ExpectedVersion",
                        "0.6.0",
                        "-ExpectedBuildId",
                        BUILD_ID,
                        "-CandidateVersionJsonPath",
                        manifest,
                    ],
                    "ExpectedSha256 is required",
                ),
                (
                    "malformed-sha",
                    [
                        "-ExpectedSha256",
                        "abc",
                        "-ExpectedVersion",
                        "0.6.0",
                        "-ExpectedBuildId",
                        BUILD_ID,
                        "-CandidateVersionJsonPath",
                        manifest,
                    ],
                    "ExpectedSha256 must be 64 hexadecimal characters",
                ),
                (
                    "mismatched-sha",
                    [
                        "-ExpectedSha256",
                        "0" * 64,
                        "-ExpectedVersion",
                        "0.6.0",
                        "-ExpectedBuildId",
                        BUILD_ID,
                        "-CandidateVersionJsonPath",
                        manifest,
                    ],
                    "candidate SHA-256 mismatch",
                ),
                (
                    "mismatched-build",
                    [
                        "-ExpectedSha256",
                        digest,
                        "-ExpectedVersion",
                        "0.6.0",
                        "-ExpectedBuildId",
                        "0.6.0+20260827.050713.deadbee",
                        "-CandidateVersionJsonPath",
                        manifest,
                    ],
                    "candidate build_id mismatch",
                ),
            ]

            for name, extra_args, expected in cases:
                with self.subTest(name=name):
                    result = self.run_runner(
                        "-SelfTestContract",
                        "identity",
                        "-ExePath",
                        exe,
                        *extra_args,
                    )
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    self.assertIn(expected, result.stdout)
                    self.assertNotIn("SELFTEST_LAUNCH", result.stdout)

    def test_matching_identity_allows_isolated_preflight_without_live_profile_access(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            exe, digest, manifest = self.make_candidate(tmp_path)
            parent_appdata = tmp_path / "parent-roaming"
            parent_local = tmp_path / "parent-local"
            (parent_appdata / "DeckPipe").mkdir(parents=True)
            (parent_appdata / "Pioneer" / "rekordbox").mkdir(parents=True)
            parent_local.mkdir()
            (parent_appdata / "DeckPipe" / "config.local.json").write_text("sentinel", encoding="utf-8")
            (parent_appdata / "Pioneer" / "rekordbox" / "master.db").write_text("sentinel", encoding="utf-8")

            result = self.run_runner(
                "-SelfTestContract",
                "isolated-preflight",
                "-IsolatedUi",
                "-ExePath",
                exe,
                "-ExpectedSha256",
                digest,
                "-ExpectedVersion",
                "0.6.0",
                "-ExpectedBuildId",
                BUILD_ID,
                "-CandidateVersionJsonPath",
                manifest,
                env={
                    "APPDATA": str(parent_appdata),
                    "LOCALAPPDATA": str(parent_local),
                },
            )

            self.assertEqual(0, result.returncode, result.stdout)
            payload = self.parse_selftest_json(result)
            self.assertTrue(payload["identity"]["matched"])
            self.assertTrue(payload["launch_allowed"])
            self.assertTrue(payload["isolated"])
            self.assertTrue(payload["config_path"].startswith(payload["isolated_root"]))
            self.assertTrue(payload["database_path"].startswith(payload["isolated_root"]))
            self.assertFalse(payload["config_path"].startswith(str(parent_appdata)))
            self.assertFalse(payload["database_path"].startswith(str(parent_appdata)))
            self.assertEqual("sentinel", (parent_appdata / "DeckPipe" / "config.local.json").read_text(encoding="utf-8"))
            self.assertEqual("sentinel", (parent_appdata / "Pioneer" / "rekordbox" / "master.db").read_text(encoding="utf-8"))

    def test_dynamic_listener_selection_accepts_only_one_owned_loopback_listener(self):
        cases = [
            ("listener-owned", 0, "53123"),
            ("listener-none", 1, "owned loopback listener was not found"),
            ("listener-multiple", 1, "multiple owned loopback listeners"),
            ("listener-unowned", 1, "owned loopback listener was not found"),
        ]
        for scenario, expected_code, expected_text in cases:
            with self.subTest(scenario=scenario):
                result = self.run_runner("-SelfTestContract", scenario, "-ValidateOnly")
                if expected_code == 0:
                    self.assertEqual(0, result.returncode, result.stdout)
                else:
                    self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn(expected_text, result.stdout)

    def test_validate_only_is_ps51_parseable_and_has_no_fixed_candidate_or_port_default(self):
        result = self.run_runner("-ValidateOnly", "-IsolatedUi")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("VALID installed-runner mode=isolated-ui endpoints=9 samples=5", result.stdout)

        source = RUNNER.read_text(encoding="ascii")
        self.assertNotIn("E:\\DeckPipe", source)
        self.assertNotIn("24680", source)


if __name__ == "__main__":
    unittest.main()
