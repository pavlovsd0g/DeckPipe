# DeckPipe 0.6.0 release pipeline

This directory contains the Windows release evidence tooling for DeckPipe 0.6.0.

`version.json` is the canonical source for the release version and opaque build ID. The build ID is stable release metadata, not source provenance. `release/build.ps1` records the exact current `git rev-parse HEAD` as `source_revision` in `release-evidence.json` at build time, and artifact names include both the canonical build ID and current source revision. `release/verify.ps1` also binds normal CLI verification to `release/version.json` next to the verifier script and the current repository `HEAD`; stale evidence for another build ID or source revision cannot pass.

The final staging directory is closed and top-level only. It may contain only:

- `DeckPipe-<build_id>-<source7>-x64.exe` (the application executable that must be byte-identical to the installed `deckpipe.exe`)
- `DeckPipe-<build_id>-<source7>-x64-setup.exe`
- `DeckPipe-<build_id>-<source7>-x64.msi`
- `policy.json` only for the owner-waived private-beta channel
- `release-evidence.json`
- `sbom.spdx.json`
- `SHA256SUMS.txt`

`release-evidence.json` is the machine-consumable candidate binding contract:

- `version`, `build_id`, and full 40-character `source_revision`.
- A nonempty `artifacts[]` set with relative top-level `path`, exact extension-derived `type` (`exe` or `msi`), and `sha256`.
- `signing.status` and `timestamp.status`, each `PASS` only after explicit thumbprint signing and RFC3161 timestamp verification.
- `signing.signed[]` must exactly equal the artifact path set; empty or omitted artifact/signing sets are malformed.
- Private beta is the only unsigned release exception. It requires `distribution.channel: "private-beta"`, `distribution.artifact_label: "unsigned-private-beta"`, `distribution.policy_path: "policy.json"`, a lowercase `distribution.policy_sha256`, `signing.status` and `timestamp.status` set to `WAIVED_BY_OWNER`, empty `signing.signed[]`, and `policy.json` byte-for-byte identical to tracked `release/policy.json`. Any public, absent, drifted, extra-field, caller-path, or forged-hash policy restores the normal Authenticode/timestamp requirement or fails verification.
- `SHA256SUMS.txt` hashes every staged file except itself, exactly covering `release-evidence.json`, `sbom.spdx.json`, and every artifact.
- `sbom.spdx.json` is SPDX 2.3 and must describe `release-evidence.json`, optional private-beta `policy.json`, and the exact artifact set through one `DOCUMENT DESCRIBES Package` relationship and exactly one `Package CONTAINS File` relationship per file. Extra SPDX relationships are release failures.

Current status:

- Python runtime and build dependency locks are hash-complete for Windows x64 CPython 3.12. Regenerate them only through `release/prepare-wheelhouse.ps1` with `-LabRoot D:\DeckPipe-RC-Lab`, the project CPython 3.12 executable, and the pinned `pip-tools==7.6.1` generator.
- `release/prepare-wheelhouse.ps1` creates and uses only the fixed D-drive lab layout: `downloads`, `wheelhouse`, `tool-cache`, `build`, `staging`, `install`, `backups\rekordbox`, `qa-evidence`, and `logs-redacted`. It records a redacted host/tool inventory under `qa-evidence`, compiles both locks with hashes, downloads only binary wheels, writes `wheelhouse\wheelhouse-manifest.json`, and proves a clean offline install from that wheelhouse in `D:\DeckPipe-RC-Lab\tool-cache\offline-proof` for Task 11 reuse.
- `release/build.ps1` refuses to stage artifacts unless both Python lock files contain `--require-hashes` and real hashes, the caller-supplied Python is an existing Windows x64 CPython 3.12 AMD64/64-bit executable, and the explicit wheelhouse manifest exactly matches the wheel files by relative name, size, and lowercase SHA-256. The wheelhouse must live below `D:\DeckPipe-RC-Lab`, outside the repository, and outside staging.
- Builds export tracked `HEAD` into a unique D-drive lab build workspace outside both repo and staging, install both Python lock files with `--no-cache-dir --no-index --find-links --require-hashes`, run npm/Cargo offline with lab/temp output paths, copy the verified wheelhouse manifest hash into `release-evidence.json`, assemble artifacts/evidence/SBOM/manifest in an owned candidate directory outside the repo and outside final staging, verify that complete candidate, then publish it by same-volume directory rename into an absent final staging directory. If the caller supplied an existing empty staging directory, it is removed non-recursively only at the final publish step.
- A build, signing, evidence, SBOM, verifier, or crash failure before publish cleans only owned temp/candidate paths and leaves final staging absent or empty. Signed builds require verifier `PASS` before publish. Unsigned engineering candidates may publish only as structurally valid non-release candidates with explicit signing/timestamp `BLOCKED` evidence. Private-beta candidates use `-PrivateBetaCandidate`, cannot combine with signing inputs or `-UnsignedEngineeringCandidate`, and add `-unsigned-private-beta` to every artifact basename.
- Signing and timestamping require explicit `-SignToolPath`, certificate selector, and `-TimestampUrl`; the scripts never enumerate certificate stores or use automatic certificate selection.
- `release/verify.ps1` writes one JSON result to stdout. `PASS` exits 0, `FAIL` exits 1, and `BLOCKED` exits 2; callers must treat anything except parsed `status: "PASS"` as non-release evidence.
