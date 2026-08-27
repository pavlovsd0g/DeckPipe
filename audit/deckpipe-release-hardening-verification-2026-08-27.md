# DeckPipe 0.6.0 release-hardening verification - 2026-08-27

## Decision

Engineering implementation is verified for the source, synthetic, and offline
boundaries covered by the plan.

Release publication is NO-GO. The candidate is not releasable until the
external release-candidate gates listed below are executed and pass with fresh
evidence.

No live user credentials, browser auth stores, live media, live Rekordbox
database, Windows services, certificate stores, or external network accounts
were read or mutated during this verification pass.

## Scope

- Plan: `docs/superpowers/plans/2026-08-27-deckpipe-release-hardening.md`
- Handoff: `audit/deckpipe-release-hardening-handoff-2026-08-27.md`
- Base checkpoint: `b6b3eb4`
- Verified HEAD: `01094c1a5dc175c77f01bb9be97b545e29496ec7`
- Branch: `codex/release-hardening`
- Preserved 0.5.0 tag: `backup/deckpipe-v0.5.0-pre-hardening-20260827`
- Worktree: `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`

## Verified implementation surface

- Loopback API security: exact host/origin/fetch-metadata checks, bearer
  authentication for non-public routes, generic errors, and no wildcard CORS.
- Desktop lifecycle: bundled Tauri assets, strict CSP, narrow IPC for
  backend connection data, sidecar token through child environment, retained
  child handle, teardown, parent watchdog, and single-instance behavior.
- Secret handling: Windows DPAPI-backed store, synthetic-only migration tests,
  plaintext credential removal from local config flow, and extension cookie /
  localhost bridge retirement.
- State and media safety: same-directory temporary writes, flush/fsync,
  atomic replace, recoverable backups, durable job journal, fail-closed
  lifecycle transitions, and indexed library scans.
- Rekordbox boundary: pure planning, fake-adapter apply/reconcile/rollback
  tests, explicit dry-run default, and live apply gated by experimental mode
  plus explicit confirmation.
- Frontend: canonical CSP-compatible source, generated static and desktop
  assets, safe DOM construction, keyboard/a11y contracts, responsive minimum
  window checks, and truthful Rekordbox dry-run/apply/reconcile states.
- Release tooling: canonical `release/version.json`, locked metadata,
  clean isolated build workspace logic, strict artifact allowlist, manifest /
  SPDX evidence validation, signature/timestamp fail-closed gates, PowerShell
  5.1 compatibility, Git provenance hardening, and atomic staging behavior.

## Combined verification evidence

The combined suite was run once from a clean tracked tree after one
deterministic frontend build. A later QA-only PowerShell 5.1 compatibility fix
changed only `qa/DeckPipe.QA.psm1`,
`qa/tests/DeckPipe.QA.Tests.ps1`, and
`qa/tests/DeckPipe.Orchestrator.Tests.ps1`; only the affected PowerShell gates
were rerun after that bounded fix.

| Gate | Evidence |
|---|---|
| Frontend build | `npm run build:frontend` exit 0 |
| Python repo tests | `python -m unittest discover -s qa\tests -p test_*.py`: 152/152 PASS |
| Python compile | `python -m compileall app qa`: PASS |
| Release PowerShell tests | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1`: 28/28 PASS |
| Frontend contract | 25/25 PASS before the QA-only PS5.1 fix |
| Windows PowerShell QA | `qa/tests/DeckPipe.QA.Tests.ps1`: 9/9 PASS after `01094c1` |
| Windows PowerShell orchestrator | `qa/tests/DeckPipe.Orchestrator.Tests.ps1`: 12/12 PASS after `01094c1` |
| Rust | `cargo check --locked --offline` exit 0; `cargo test --locked --offline` exit 0 |
| Node syntax | `dev.js`, `frontend/build.mjs`, `frontend/app.js`, `app/static/app.js`, `desktop/ui/app.js`: exit 0 |
| Dependency integrity | root and desktop `npm ls --all --offline` exit 0 with only expected unmet optional non-Windows packages; repo venv `pip check` clean |
| Performance | `qa/benchmark_library_scan.py` PASS: missing 158.233 ms, adoption 177.286 ms, warm sidecar 230.294 ms |
| Whitespace | `git diff --check b6b3eb4..HEAD` PASS |

Generated asset SHA-256:

| Asset pair | SHA-256 |
|---|---|
| `app/static/app.js`, `desktop/ui/app.js` | `0a30080d40048e41543e71906edd73dc994bb9c400d85d4a5425137360cee895` |
| `app/static/index.html`, `desktop/ui/index.html` | `12c6100b9824b01306b5c4b2750aa6da0ebf66df822c2c3705980cdda33a2e98` |
| `app/static/styles.css`, `desktop/ui/styles.css` | `f58f34a366100d67de3714a4c19e966116e291d8d92ab44fb9355464aa4b6f71` |

## Security review summary

Manual source/security inventory found no tracked credential-bearing file
types or release-forbidden database/key/config artifacts:

- no tracked `.db`, `.sqlite`, `.pfx`, `.p12`, `.pem`, `.key`, `.env`,
  `config.local.json`, `master.db`, or media artifact matches;
- hostile origin/auth/XSS/extension source reproducers are included in the
  152/152 Python PASS;
- broad unsafe-source matches were limited to negative tests and guarded
  release/temp cleanup paths;
- no high-confidence new secret-output path was found in the branch diff.

Residual risk: `app/deezer_client.py` still contains a hardcoded Deezer
provider client secret reference. It appears twice at HEAD and appeared twice
at `b6b3eb4`, so it is pre-existing relative to this hardening branch. Its
value was not printed into this report or verification output. This is not a
new release-hardening regression, but it remains a product security item that
should be removed or replaced with a documented non-secret public-client
contract before a public distribution decision.

Official Codex Security standard/diff/deep scans are BLOCKED in this surface:
the handoff states that `codex-security` is installed and enabled, but the live
Codex tool surface exposes no callable scanner and the plugin is listed as
available but not installed. No plugin installation was authorized during this
run.

## Independent review

Task-scoped independent reviews passed for Tasks 1-7 after their bounded fix
rounds. The final whole-branch review for `b6b3eb4..01094c1` returned:

- P0 findings: none
- P1 findings: none
- P2 findings: none
- SPEC verdict: PASS for the implemented engineering/source boundary
- CODE verdict: PASS for the implemented engineering/source boundary

The review did not convert unavailable build/signing/install/real Rekordbox
evidence into code defects; those remain explicit external release blockers.

## Release-candidate gates

| Gate | Status | Blocking input or authority |
|---|---:|---|
| Hash-complete Python runtime and build locks / offline wheelhouse | BLOCKED | `requirements.lock` and `requirements-build.lock` intentionally retain blocked lock status; no real hash-complete wheelhouse supplied |
| Real Windows build | BLOCKED | no authorized execution of the full PyInstaller/Tauri build was performed |
| Authenticode signing | BLOCKED | no authorized SignTool path or certificate thumbprint supplied |
| RFC3161 timestamp | BLOCKED | no authorized timestamp URL supplied |
| Final staged artifacts, SHA-256 manifest, SPDX SBOM, package allowlist | BLOCKED | no real built and signed release stage exists |
| Clean install and first launch | BLOCKED | no current 0.6.0 candidate installed in a clean profile/machine |
| Upgrade from preserved 0.5.0 | BLOCKED | no current 0.6.0 candidate installed; preserved 0.5.0 artifact remains unchanged |
| Uninstall/reinstall and orphan-process cleanup | BLOCKED | no current 0.6.0 candidate installed |
| Installed EXE hostile origin/auth/XSS/extension reproducers | BLOCKED | no current 0.6.0 candidate installed |
| Real network behavior | BLOCKED | no external account/network authorization in this run |
| Disposable-copy Rekordbox Unicode/apply audit | BLOCKED | no explicit disposable Rekordbox copy path supplied |
| Official Codex Security deep release-candidate scan | BLOCKED | no callable scanner and no release candidate |

## Final status

Engineering hardening is complete and verified at the source/synthetic/offline
boundary.

Release remains NO-GO. Do not publish or call the 0.6.0 candidate releasable
until every mandatory RC-only gate above has PASS evidence from the actual
built, signed, staged, and installed candidate.
