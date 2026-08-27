import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "qa" / "run-installed-qa.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
BUILD_ID = "0.6.0+20260827.050713.6456dba254a6"
SOURCE_REVISION = "46ef5d8f1578412c500cc9da13472f9fb2412cab"
ARTIFACT_NAME = f"DeckPipe-{BUILD_ID}-{SOURCE_REVISION[:7]}-x64.exe"


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

    def run_runner_with_parent_prelude(self, prelude: str, *args, env=None):
        quoted_runner = "'" + str(RUNNER).replace("'", "''") + "'"
        rendered_args = []
        for arg in args:
            arg = str(arg)
            if arg.startswith("-"):
                rendered_args.append(arg)
            else:
                rendered_args.append("'" + arg.replace("'", "''") + "'")
        quoted_args = " ".join(rendered_args)
        parent_script = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".ps1",
            prefix="deckpipe-parent-prelude-",
            delete=False,
        )
        parent_script.write(
            "$ErrorActionPreference = 'Stop'\n"
            + prelude
            + "\n"
            + f"& {quoted_runner} {quoted_args}\n"
            + "exit $LASTEXITCODE\n"
        )
        parent_script.close()
        command = [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            parent_script.name,
        ]
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            return subprocess.run(
                command,
                cwd=ROOT,
                env=merged_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
        finally:
            Path(parent_script.name).unlink(missing_ok=True)

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_json(self, path: Path, payload) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def make_stage(
        self,
        directory: Path,
        *,
        artifact_bytes: bytes = b"synthetic deckpipe executable\n",
        status: str = "BLOCKED",
        artifact_name: str = ARTIFACT_NAME,
        second_artifact_same_bytes: bool = False,
    ):
        stage = directory / "stage"
        stage.mkdir()
        artifact = stage / artifact_name
        artifact.write_bytes(artifact_bytes)
        artifacts = [{"path": artifact_name, "type": "exe", "sha256": self.sha256(artifact)}]
        files = [artifact_name]
        if second_artifact_same_bytes:
            second = stage / f"DeckPipe-{BUILD_ID}-{SOURCE_REVISION[:7]}-copy-x64.exe"
            second.write_bytes(artifact_bytes)
            artifacts.append({"path": second.name, "type": "exe", "sha256": self.sha256(second)})
            files.append(second.name)

        evidence = {
            "schema_version": 1,
            "product": "DeckPipe",
            "version": "0.6.0",
            "build_id": BUILD_ID,
            "source_revision": SOURCE_REVISION,
            "artifacts": artifacts,
            "signing": {"status": status, "signed": [a["path"] for a in artifacts]},
            "timestamp": {"status": status},
        }
        self.write_json(stage / "release-evidence.json", evidence)
        files.append("release-evidence.json")

        relationships = [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-Package-DeckPipe",
            }
        ]
        sbom_files = []
        for relative in sorted(files):
            full = stage / relative
            spdx_id = "SPDXRef-File-" + hashlib.sha256((relative + "\0" + self.sha256(full)).encode()).hexdigest()[:32]
            sbom_files.append(
                {
                    "SPDXID": spdx_id,
                    "fileName": relative,
                    "checksums": [{"algorithm": "SHA256", "checksumValue": self.sha256(full)}],
                    "licenseConcluded": "NOASSERTION",
                    "copyrightText": "NOASSERTION",
                }
            )
            relationships.append(
                {
                    "spdxElementId": "SPDXRef-Package-DeckPipe",
                    "relationshipType": "CONTAINS",
                    "relatedSpdxElement": spdx_id,
                }
            )

        sbom = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"DeckPipe-{BUILD_ID}",
            "documentNamespace": f"https://deckpipe.local/spdx/{BUILD_ID}",
            "creationInfo": {"created": "2026-08-27T05:07:13Z", "creators": ["Tool: test"]},
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-DeckPipe",
                    "name": "DeckPipe",
                    "versionInfo": "0.6.0",
                    "downloadLocation": "NOASSERTION",
                    "filesAnalyzed": True,
                    "licenseConcluded": "NOASSERTION",
                    "licenseDeclared": "NOASSERTION",
                    "copyrightText": "NOASSERTION",
                }
            ],
            "files": sbom_files,
            "relationships": relationships,
        }
        self.write_json(stage / "sbom.spdx.json", sbom)

        manifest_files = sorted(files + ["sbom.spdx.json"])
        (stage / "SHA256SUMS.txt").write_text(
            "".join(f"{self.sha256(stage / relative)}  {relative}\n" for relative in manifest_files),
            encoding="utf-8",
        )
        return stage, artifact

    def make_installed_copy(self, directory: Path, staged_artifact: Path):
        install_dir = directory / "installed" / "DeckPipe"
        install_dir.mkdir(parents=True)
        installed = install_dir / "DeckPipe.exe"
        shutil.copyfile(staged_artifact, installed)
        return installed

    def parse_selftest_json(self, result):
        for line in result.stdout.splitlines():
            if line.startswith("SELFTEST_JSON "):
                return json.loads(line[len("SELFTEST_JSON ") :])
        self.fail(f"SELFTEST_JSON line missing from output:\n{result.stdout}")

    def test_spoofed_notepad_hash_and_fake_version_manifest_cannot_authorize_launch(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            fake_notepad = tmp_path / "notepad.exe"
            fake_notepad.write_bytes(b"notepad bytes with caller-computed hash\n")
            fake_manifest = tmp_path / "version.json"
            self.write_json(fake_manifest, {"version": "0.6.0", "build_id": BUILD_ID})

            result = self.run_runner(
                "-SelfTestContract",
                "identity",
                "-ExePath",
                fake_notepad,
                "-ExpectedSha256",
                self.sha256(fake_notepad),
                "-ExpectedVersion",
                "0.6.0",
                "-ExpectedBuildId",
                BUILD_ID,
                "-CandidateVersionJsonPath",
                fake_manifest,
            )

            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("CandidateEvidenceDirectory is required", result.stdout)
            self.assertNotIn("SELFTEST_LAUNCH", result.stdout)
            self.assertNotIn("launch_allowed", result.stdout)

    def test_fake_pass_evidence_without_valid_signature_and_timestamp_stops_before_launch(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            stage, artifact = self.make_stage(tmp_path, status="PASS")
            installed = self.make_installed_copy(tmp_path, artifact)

            result = self.run_runner(
                "-SelfTestContract",
                "identity",
                "-ExePath",
                installed,
                "-CandidateEvidenceDirectory",
                stage,
            )

            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("release verifier status", result.stdout)
            self.assertNotIn("SELFTEST_LAUNCH", result.stdout)
            self.assertNotIn("launch_allowed", result.stdout)

    def test_parent_injected_authenticode_function_cannot_authorize_unsigned_evidence(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            stage, artifact = self.make_stage(tmp_path, status="PASS")
            installed = self.make_installed_copy(tmp_path, artifact)
            prelude = """
function Get-AuthenticodeSignature {
    param([string]$LiteralPath)
    [pscustomobject]@{
        Status = 'Valid'
        TimeStamperCertificate = [pscustomobject]@{ Subject = 'CN=Injected Test TSA'; Thumbprint = 'ABC123' }
    }
}
"""

            result = self.run_runner_with_parent_prelude(
                prelude,
                "-SelfTestContract",
                "identity",
                "-ExePath",
                installed,
                "-CandidateEvidenceDirectory",
                stage,
            )

            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("release verifier status", result.stdout)
            self.assertRegex(result.stdout, r"Authenticode|Get-AuthenticodeSignature|signature")
            self.assertNotIn("SELFTEST_LAUNCH", result.stdout)
            self.assertNotIn("launch_allowed", result.stdout)

    def test_caller_cannot_supply_a_fake_release_verifier_path(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            fake_verifier = tmp_path / "verify.ps1"
            fake_verifier.write_text(
                "Write-Output '{\"schema_version\":1,\"status\":\"PASS\",\"message\":\"fake\"}'\nexit 0\n",
                encoding="utf-8",
            )

            result = self.run_runner(
                "-ValidateOnly",
                "-ReleaseVerifierPath",
                fake_verifier,
            )

            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("parameter", result.stdout.lower())

    def test_verifier_process_rejects_noisy_empty_multiple_and_exit_status_mismatches(self):
        result = self.run_runner("-SelfTestContract", "verifier-process-contract", "-ValidateOnly")
        self.assertEqual(0, result.returncode, result.stdout)
        payload = self.parse_selftest_json(result)
        self.assertEqual(
            {
                "pass": "PASS:0",
                "fail": "FAIL:1",
                "blocked": "BLOCKED:2",
                "empty": "REJECTED",
                "multiple": "REJECTED",
                "noise": "REJECTED",
                "pass_stderr": "REJECTED",
                "pass_exit_1": "REJECTED",
                "fail_exit_0": "REJECTED",
                "blocked_exit_0": "REJECTED",
            },
            payload["cases"],
        )

    def test_evidence_manifest_and_sbom_tampering_are_rejected_before_launch(self):
        def remove_contains(stage: Path):
            sbom = json.loads((stage / "sbom.spdx.json").read_text(encoding="utf-8"))
            sbom["relationships"] = [r for r in sbom["relationships"] if r.get("relationshipType") != "CONTAINS"]
            self.write_json(stage / "sbom.spdx.json", sbom)
            manifest_lines = []
            for line in (stage / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
                if line.endswith("  sbom.spdx.json"):
                    manifest_lines.append(f"{self.sha256(stage / 'sbom.spdx.json')}  sbom.spdx.json")
                else:
                    manifest_lines.append(line)
            (stage / "SHA256SUMS.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

        cases = {
            "missing-evidence": lambda stage: (stage / "release-evidence.json").unlink(),
            "extra-file": lambda stage: (stage / "extra.txt").write_text("extra", encoding="utf-8"),
            "manifest-traversal": lambda stage: (stage / "SHA256SUMS.txt").write_text(
                (stage / "SHA256SUMS.txt").read_text(encoding="utf-8").replace("  release-evidence.json", "  ../evil.json"),
                encoding="utf-8",
            ),
            "sbom-contains": remove_contains,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
                tmp_path = Path(tmp)
                stage, artifact = self.make_stage(tmp_path)
                installed = self.make_installed_copy(tmp_path, artifact)
                mutate(stage)

                result = self.run_runner(
                    "-SelfTestContract",
                    "identity",
                    "-AllowUnsignedEngineeringEvidence",
                    "-ExePath",
                    installed,
                    "-CandidateEvidenceDirectory",
                    stage,
                )

                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertNotIn("SELFTEST_LAUNCH", result.stdout)
                self.assertNotIn("launch_allowed", result.stdout)

    def test_unique_staged_executable_binds_byte_identical_installed_copy_only_in_blocked_engineering_mode(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            stage, artifact = self.make_stage(tmp_path)
            installed = self.make_installed_copy(tmp_path, artifact)
            result = self.run_runner(
                "-SelfTestContract",
                "isolated-preflight",
                "-IsolatedUi",
                "-AllowUnsignedEngineeringEvidence",
                "-ExePath",
                installed,
                "-CandidateEvidenceDirectory",
                stage,
                env={
                    "APPDATA": str(tmp_path / "parent-roaming"),
                    "LOCALAPPDATA": str(tmp_path / "parent-local"),
                },
            )

            self.assertEqual(0, result.returncode, result.stdout)
            payload = self.parse_selftest_json(result)
            self.assertTrue(payload["identity"]["matched"])
            self.assertFalse(payload["launch_allowed"])
            self.assertEqual("BLOCKED", payload["identity"]["release_verifier_status"])
            self.assertEqual(str(artifact), payload["identity"]["staged_artifact"])
            self.assertEqual(str(installed), payload["identity"]["resolved_exe"])
            self.assertTrue(payload["config_path"].startswith(payload["isolated_root"]))
            self.assertTrue(payload["database_path"].startswith(payload["isolated_root"]))

    def test_unsigned_engineering_evidence_stops_normal_runner_before_process_start(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            stage, artifact = self.make_stage(tmp_path)
            installed = self.make_installed_copy(tmp_path, artifact)

            result = self.run_runner(
                "-AllowUnsignedEngineeringEvidence",
                "-IsolatedUi",
                "-ExePath",
                installed,
                "-CandidateEvidenceDirectory",
                stage,
            )

            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("QA_RESULT status=blocked", result.stdout)
            self.assertIn("RELEASE", result.stdout)
            self.assertNotIn("not a valid Win32 application", result.stdout)
            self.assertNotIn("SELFTEST_LAUNCH", result.stdout)

    def test_duplicate_matching_staged_executables_are_ambiguous(self):
        with tempfile.TemporaryDirectory(prefix="deckpipe-runner-contract-") as tmp:
            tmp_path = Path(tmp)
            stage, artifact = self.make_stage(tmp_path, second_artifact_same_bytes=True)
            installed = self.make_installed_copy(tmp_path, artifact)

            result = self.run_runner(
                "-SelfTestContract",
                "identity",
                "-AllowUnsignedEngineeringEvidence",
                "-ExePath",
                installed,
                "-CandidateEvidenceDirectory",
                stage,
            )

            self.assertNotEqual(0, result.returncode, result.stdout)
            self.assertIn("ambiguous executable artifact", result.stdout)
            self.assertNotIn("SELFTEST_LAUNCH", result.stdout)

    def test_runner_reparse_attribute_guard_rejects_every_candidate_boundary(self):
        result = self.run_runner("-SelfTestContract", "reparse-attribute-guard", "-ValidateOnly")
        self.assertEqual(0, result.returncode, result.stdout)
        payload = self.parse_selftest_json(result)
        self.assertEqual(
            {
                "candidate_evidence_directory": "REJECTED",
                "evidence_top_level_entry": "REJECTED",
                "staged_artifact": "REJECTED",
                "installed_exe": "REJECTED",
                "install_directory": "REJECTED",
                "ordinary_file": "ACCEPTED",
            },
            payload["cases"],
        )

    def test_engineering_blocked_evidence_can_never_aggregate_pass(self):
        result = self.run_runner("-SelfTestContract", "mandatory-skips", "-ValidateOnly")
        self.assertEqual(0, result.returncode, result.stdout)
        payload = self.parse_selftest_json(result)
        self.assertEqual("blocked", payload["summary"]["status"])
        self.assertEqual(["A3", "D2"], payload["summary"]["blockers"])

    def test_dynamic_listener_selection_revalidates_owned_path_and_parent_chain(self):
        cases = [
            ("listener-owned", 0, "53123"),
            ("listener-none", 1, "owned loopback listener was not found"),
            ("listener-multiple", 1, "multiple owned loopback listeners"),
            ("listener-unowned", 1, "owned loopback listener was not found"),
            ("listener-wrong-path", 1, "owned loopback listener was not found"),
            ("listener-pid-reuse", 1, "listener ownership changed before accept"),
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
