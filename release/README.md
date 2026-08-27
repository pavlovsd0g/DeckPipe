# DeckPipe 0.6.0 release pipeline

This directory contains the Windows release evidence tooling for DeckPipe 0.6.0.

`version.json` is the canonical source for the release version and opaque build ID. The build ID is stable release metadata, not source provenance. `release/build.ps1` records the exact current `git rev-parse HEAD` as `source_revision` in `release-evidence.json` at build time, and artifact names include both the build ID and source revision.

`release-evidence.json` is the machine-consumable candidate binding contract:

- `version`, `build_id`, and full 40-character `source_revision`.
- `artifacts[]` entries with relative `path`, `type` (`exe`, `msi`, or `installer`), and `sha256`.
- `signing.status` and `timestamp.status`, each `PASS` only after explicit thumbprint signing and RFC3161 timestamp verification.
- `SHA256SUMS.txt` hashes every staged file except itself, including `release-evidence.json`, `sbom.spdx.json`, and every artifact.
- `sbom.spdx.json` is SPDX 2.3 and must describe the same artifact/evidence set through `DOCUMENT DESCRIBES Package` and `Package CONTAINS File` relationships.

Current status:

- Python runtime and build dependency hashes are BLOCKED. The project venv has pinned versions, but the local offline wheel cache does not contain every required distribution. The lock files intentionally do not invent hashes.
- `release/build.ps1` refuses to stage artifacts while Python locks are blocked.
- Signing and timestamping require explicit `-SignToolPath`, certificate selector, and `-TimestampUrl`; the scripts never enumerate certificate stores or use automatic certificate selection.
- `release/verify.ps1` fails closed unless SHA-256 manifest, SPDX 2.3 SBOM, clean staged contents, and timestamped Authenticode evidence are present.
