# DeckPipe 0.6.0 release pipeline

This directory contains the Windows release evidence tooling for DeckPipe 0.6.0.

`version.json` is the canonical source for the release version and opaque build ID. The build ID is stable release metadata, not source provenance. `release/build.ps1` records the exact current `git rev-parse HEAD` as `source_revision` in `release-evidence.json` at build time, and artifact names include both the build ID and source revision.

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
- `sbom.spdx.json` is SPDX 2.3 and must describe `release-evidence.json` plus the exact artifact set through `DOCUMENT DESCRIBES Package` and `Package CONTAINS File` relationships.

Current status:

- Python runtime and build dependency hashes are BLOCKED. The project venv has pinned versions, but the local offline wheel cache does not contain every required distribution. The lock files intentionally do not invent hashes.
- `release/build.ps1` refuses to stage artifacts while Python locks are blocked. After locks are ready, it requires an explicit offline wheelhouse, exports tracked `HEAD` into a unique temp source directory outside both repo and staging, installs both Python lock files with `--no-index --require-hashes`, runs npm/Cargo offline with temp output paths, and copies only the canonical app/setup/MSI artifacts into staging before evidence inventory.
- Signing and timestamping require explicit `-SignToolPath`, certificate selector, and `-TimestampUrl`; the scripts never enumerate certificate stores or use automatic certificate selection.
- `release/verify.ps1` writes one JSON result to stdout. `PASS` exits 0, `FAIL` exits 1, and `BLOCKED` exits 2; callers must treat anything except parsed `status: "PASS"` as non-release evidence.
