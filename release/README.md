# DeckPipe 0.6.0 release pipeline

This directory contains the Windows release evidence tooling for DeckPipe 0.6.0.

`version.json` is the canonical source for the release version and build ID. Package metadata, Tauri metadata, the FastAPI version endpoint, bug reports, artifact names, checksum manifests, and SBOM output must resolve to this file.

Current status:

- Python runtime and build dependency hashes are BLOCKED. The project venv has pinned versions, but the local offline wheel cache does not contain every required distribution. The lock files intentionally do not invent hashes.
- `release/build.ps1` refuses to stage artifacts while Python locks are blocked.
- Signing and timestamping require explicit `-SignToolPath`, certificate selector, and `-TimestampUrl`; the scripts never enumerate certificate stores or use automatic certificate selection.
- `release/verify.ps1` fails closed unless SHA-256 manifest, SPDX 2.3 SBOM, clean staged contents, and timestamped Authenticode evidence are present.
