# DeckPipe 0.6.0 release pipeline

This directory contains the Windows release evidence tooling for DeckPipe 0.6.0.

`version.json` is the canonical source for the release version and opaque build ID. The build ID is stable release metadata, not source provenance. `release/build.ps1` records the exact current `git rev-parse HEAD` as `source_revision` in `release-evidence.json` at build time, and artifact names include both the canonical build ID and current source revision. `release/verify.ps1` also binds normal CLI verification to `release/version.json` next to the verifier script and the current repository `HEAD`; stale evidence for another build ID or source revision cannot pass.

The final staging directory is closed and top-level only. It may contain only:

- `DeckPipe-<build_id>-<source7>-x64.exe` (the application executable that must be byte-identical to the installed `deckpipe.exe`)
- `DeckPipe-<build_id>-<source7>-x64-setup.exe`
- `DeckPipe-<build_id>-<source7>-x64.msi`
- `release-evidence.json`
- `sbom.spdx.json`
- `SHA256SUMS.txt`

`release-evidence.json` is the machine-consumable candidate binding contract:

- `version`, `build_id`, and full 40-character `source_revision`.
- A nonempty `artifacts[]` set with relative top-level `path`, exact extension-derived `type` (`exe` or `msi`), and `sha256`.
- `signing.status` and `timestamp.status`, each `PASS` only after explicit thumbprint signing and RFC3161 timestamp verification.
- `signing.signed[]` must exactly equal the artifact path set; empty or omitted artifact/signing sets are malformed.
- `SHA256SUMS.txt` hashes every staged file except itself, exactly covering `release-evidence.json`, `sbom.spdx.json`, and every artifact.
- `sbom.spdx.json` is SPDX 2.3 and must describe `release-evidence.json` plus the exact artifact set through one `DOCUMENT DESCRIBES Package` relationship and exactly one `Package CONTAINS File` relationship per file. Extra SPDX relationships are release failures.

Current status:

- Python runtime and build dependency hashes are BLOCKED. The project venv has pinned versions, but the local offline wheel cache does not contain every required distribution. The lock files intentionally do not invent hashes.
- `release/build.ps1` refuses to stage artifacts while Python locks are blocked. This pure version/lock preflight runs before staging path validation, dependency tooling, git archive, signing, or final directory creation.
- After locks are ready, it requires an explicit offline wheelhouse, exports tracked `HEAD` into a unique temp source directory outside both repo and staging, installs both Python lock files with `--no-index --require-hashes`, runs npm/Cargo offline with temp output paths, assembles artifacts/evidence/SBOM/manifest in an owned candidate directory outside the repo and outside final staging, verifies that complete candidate, then publishes it by same-volume directory rename into an absent final staging directory. If the caller supplied an existing empty staging directory, it is removed non-recursively only at the final publish step.
- A build, signing, evidence, SBOM, verifier, or crash failure before publish cleans only owned temp/candidate paths and leaves final staging absent or empty. Signed builds require verifier `PASS` before publish. Unsigned engineering candidates may publish only as structurally valid non-release candidates with explicit signing/timestamp `BLOCKED` evidence.
- Signing and timestamping require explicit `-SignToolPath`, certificate selector, and `-TimestampUrl`; the scripts never enumerate certificate stores or use automatic certificate selection.
- `release/verify.ps1` writes one JSON result to stdout. `PASS` exits 0, `FAIL` exits 1, and `BLOCKED` exits 2; callers must treat anything except parsed `status: "PASS"` as non-release evidence.
