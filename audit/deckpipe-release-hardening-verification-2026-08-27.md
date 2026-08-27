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
- Verified implementation HEAD: `ff7989873b5c283ff4ce26e118ea62edc12760e8`
- Branch: `codex/release-hardening`
- Preserved 0.5.0 tag: `backup/deckpipe-v0.5.0-pre-hardening-20260827`
- Worktree: `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`

## Verified implementation surface

- Loopback API security: exact host/origin/fetch-metadata checks, bearer
  authentication for non-public routes, generic errors, and no wildcard CORS.
- Desktop lifecycle: bundled Tauri assets, strict CSP, narrow IPC for
  backend connection data, sidecar token through child environment, retained
  child handle, teardown, parent watchdog, and single-instance behavior.
- Secret handling: Windows DPAPI-backed ARL, SoundCloud OAuth, and Telegram bot
  token records; synthetic-only nested legacy migration; plaintext credential
  rejection/redaction; removal of the distributable Deezer password/client-
  secret flow; and extension cookie / localhost bridge retirement.
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

The combined suite was first run from a clean tracked tree after one
deterministic frontend build. A QA-only PowerShell 5.1 compatibility fix then
reran only its two affected PowerShell gates. The final security fix changed a
shared auth/storage/UI contract, so the plan-required combined suite was run
once more at `ff79898`.

| Gate | Evidence |
|---|---|
| Bounded fix matrix | `..\..\.venv\Scripts\python.exe -m unittest qa.tests.test_secure_store qa.tests.test_security_contract qa.tests.test_frontend_contract qa.tests.test_frontend_build_contract qa.tests.test_desktop_contract`: 56/56 PASS |
| Python repo tests | `..\..\.venv\Scripts\python.exe -m unittest discover -s qa\tests -p test_*.py`: 156/156 PASS |
| Python compile | `..\..\.venv\Scripts\python.exe -m compileall app qa`: PASS |
| Release PowerShell tests | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1`: 28/28 PASS |
| Windows PowerShell QA | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.QA.Tests.ps1`: 9/9 PASS |
| Windows PowerShell orchestrator | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Orchestrator.Tests.ps1`: 12/12 PASS |
| Frontend build | `npm run build:frontend` exit 0; post-build tracked tree clean |
| Rust | from `desktop/src-tauri`: `cargo check --locked --offline` and `cargo test --locked --offline` exit 0 |
| Node syntax | `node --check frontend/app.js`, `node --check app/static/app.js`, and `node --check desktop/ui/app.js`: exit 0; the initial combined pass also covered `dev.js` and `frontend/build.mjs` |
| Dependency integrity | root and desktop `npm ls --all --offline` exit 0 with only expected unmet optional non-Windows packages; repo venv `pip check` clean |
| Performance | `..\..\.venv\Scripts\python.exe qa/benchmark_library_scan.py` PASS: missing 207.186 ms, adoption 208.919 ms, warm sidecar 250.735 ms; all 200/200 outcomes and UTF-8 gate PASS |
| Whitespace | `git diff --check b6b3eb4..HEAD` PASS |

Generated asset SHA-256:

| Asset pair | SHA-256 |
|---|---|
| `app/static/app.js`, `desktop/ui/app.js` | `1274cbb65e285d36a109e773af81ea2dd09c504c1cf8f27b5e125843c38b8336` |
| `app/static/index.html`, `desktop/ui/index.html` | `370594d56978777ae833fcf178fc2d1fe9d212dee4de20fa3c6524481d87b15a` |
| `app/static/styles.css`, `desktop/ui/styles.css` | `f58f34a366100d67de3714a4c19e966116e291d8d92ab44fb9355464aa4b6f71` |

## Security review summary

Manual source/security inventory found no tracked credential-bearing file
types or release-forbidden database/key/config artifacts:

- no tracked `.db`, `.sqlite`, `.pfx`, `.p12`, `.pem`, `.key`, `.env`,
  `config.local.json`, `master.db`, or media artifact matches;
- hostile origin/auth/XSS/extension source reproducers are included in the
  156/156 Python PASS;
- broad unsafe-source matches were limited to negative tests and guarded
  release/temp cleanup paths;
- production source/generated assets contain no `DZ_CLIENT_SECRET`, legacy
  password-login helper, or `/api/login/deezer/password` route;
- nested legacy `telegram.bot_token` is moved into the DPAPI store while
  `chat_id` and other non-secret preferences remain in JSON; load/save rejects
  or redacts plaintext bot tokens;
- Telegram delivery failures return only generic messages, including when a
  synthetic hostile exception or remote body contains the token;
- no high-confidence secret-output path remains in the reviewed branch diff.

Official Codex Security standard/diff/deep scans are BLOCKED in this surface:
the handoff states that `codex-security` is installed and enabled, but the live
Codex tool surface exposes no callable scanner and the plugin is listed as
available but not installed. No plugin installation was authorized during this
run.

## Independent review

Task-scoped independent reviews passed for Tasks 1-7 after their bounded fix
rounds. The first whole-branch reviewer crossed its read-only boundary by
creating audit-only commit `e4bb20e`; because it authored the report, its
no-findings verdict was not accepted as independent evidence. No product file
changed in that commit.

A separate strict read-only review of `b6b3eb4..01094c1` found two P1 issues
(the distributable Deezer client secret/password flow and plaintext/raw-error
Telegram bot token) plus one P2 (Tauri login cancel shown as a security error).
The single plan-authorized bounded round captured eight RED failures of 56 and
fixed all three in `ff79898`.

The same strict reviewer then inspected `e4bb20e..ff79898` and the resulting
whole branch `b6b3eb4..ff79898` read-only. Final verdict:

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
| Real Windows build | BLOCKED | upstream hash-complete locks/offline wheelhouse are unavailable, so the fail-closed build preflight cannot proceed |
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

The existing installed `E:\DeckPipe\deckpipe.exe` is the stale preserved 0.5.0
application (SHA-256
`1b07f67d4cee8610cfcf7c36e2043c15505260ea70f59c95041b50ad6d67d31a`),
not fresh evidence for this source or a substitute for the blocked 0.6.0
candidate gates.

## Final status

Engineering hardening is complete and verified at the source/synthetic/offline
boundary.

Release remains NO-GO. Do not publish or call the 0.6.0 candidate releasable
until every mandatory RC-only gate above has PASS evidence from the actual
built, signed, staged, and installed candidate.
