# DeckPipe 0.6.0 Release Hardening Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Use `superpowers:subagent-driven-development` for bounded implementer/reviewer rounds and `superpowers:verification-before-completion` before any release claim.

**Goal:** Turn the audited DeckPipe 0.5.0 snapshot into a releasable Windows 0.6.0 candidate whose local API, desktop lifecycle, state writes, Rekordbox workflow, UI, and release artifacts fail closed and are independently verifiable.

**Architecture:** Ship a bundled Tauri frontend from one canonical frontend source. Tauri owns a single sidecar process and passes a fresh 256-bit bearer secret plus parent PID through the child environment, never through command-line arguments or logs. FastAPI binds only to loopback and authenticates every non-public API route while enforcing exact host/origin/fetch-metadata rules. Secrets move to a Windows DPAPI-backed store through an explicit, isolated migration path. State and media publication use same-directory temporary files, locks, fsync/flush, and atomic replace. Rekordbox changes are planned as a deterministic diff, backed up, applied through an adapter, reopened, and reconciled before DeckPipe state advances; live mutation stays disabled by default until the release-candidate copy gate passes.

**Tech Stack:** Python 3.12, FastAPI/Starlette, Uvicorn, Rust/Tauri 2, vanilla TypeScript-compatible JavaScript bundled with esbuild, PowerShell 5.1-compatible release tooling, unittest and existing QA harnesses.

**Source spec:** `audit/deckpipe-release-hardening-handoff-2026-08-27.md`

## Non-negotiable boundaries

- Never read, print, copy, rotate, delete, or rewrite the user's live Kimi, Telegram, Deezer, or SoundCloud credentials during implementation or verification.
- All credential migration tests use temporary directories and synthetic values. Merely adding migration code does not authorize executing it against `%APPDATA%\DeckPipe`.
- Never open or mutate the user's live Rekordbox database. Adapter tests use fakes; integration tests use an explicit disposable copy.
- Do not publish or call an artifact "release-ready" unless Authenticode, timestamp, checksum, SPDX SBOM, clean-install, upgrade, uninstall, installed-EXE, and copy-of-real-Rekordbox gates have evidence.
- Preserve `backup/deckpipe-v0.5.0-pre-hardening-20260827` and the verified external backup directory unchanged.
- One RED/GREEN cycle per task, one independent task review, one combined suite, one whole-branch review, and at most one bounded fix round unless a P0/P1 failure requires another cycle.

## Locked decisions and cost if wrong

| Decision | Why it is locked | Cost if wrong |
|---|---|---|
| Canonical release version is `0.6.0` | The security and behavior changes are materially larger than a patch and existing files already drift between 0.1.0/0.3.0/0.5.0. | Reissue metadata and artifact names; no user data migration is tied to the version. |
| Packaged UI is a bundled Tauri asset, not an external localhost webview | Enables strict CSP, removes remote-URL IPC capability, and makes the app origin deterministic. | Frontend build/runtime rollback only; API remains usable in browser development mode. |
| Packaged Windows origin is exactly `http://tauri.localhost` | This is Tauri 2's packaged Windows origin and is the sole cross-origin API client. | Update one allowlist and regression tests; authentication still blocks unauthenticated access. |
| Per-launch API token travels in the sidecar environment | Avoids CLI/process-list and log leakage while preserving a simple loopback API. | Replace transport with inherited pipe/native IPC; routes stay fail closed. |
| Browser extension loses cookie and localhost permissions in 0.6.0 | The current arbitrary-port probe plus cookie bridge is not defensible. Native messaging is a separate reviewed feature. | Users temporarily use the desktop login action; extension functionality can be restored only with a native host and fixed extension allowlist. |
| Rekordbox live apply is off by default | A DB commit alone cannot roll back ANLZ file writes, and real-library compatibility has not been proven. | Flip the feature flag only after disposable-copy and RC gates; dry-run remains useful. |
| Signing is a hard external release gate | No certificate or timestamping credential is available in the repository and none will be fabricated. | Engineering candidate can be built, but publication remains blocked with an explicit gate report. |

### Task 1: Enforce the loopback API security contract

**Files:**
- Create: `app/security.py`
- Modify: `app/main.py`
- Modify: `run_backend.py`
- Modify: `dev.js`
- Create: `qa/tests/test_security_contract.py`
- Modify: `qa/tests/DeckPipe.QA.Tests.ps1`

**Step 1: Write the failing contract tests**

Cover: loopback-only host validation; `/`, `/static/*`, and `/api/version` public; every other `/api/*` route requires `Authorization: Bearer`; exact `http://tauri.localhost` CORS response; no wildcard; cross-site Fetch Metadata rejection; no token values in logs or exception bodies; startup failure when the secret is absent outside an explicit test mode.

**Step 2: Run the focused RED gate**

Run: `python -m unittest qa.tests.test_security_contract -v`

Expected: FAIL because the middleware and launch secret contract do not exist.

**Step 3: Implement the minimum security boundary**

Add a pure ASGI middleware so tests do not require a new HTTP client dependency. Validate `Host` before routing, reject hostile `Origin`/`Referer`/`Sec-Fetch-Site`, authenticate sensitive routes with constant-time comparison, return generic JSON errors, and attach restrictive response headers. Configure CORS with the one packaged origin, explicit methods/headers, and no credentials. Make `run_backend.py` read the secret from `DECKPIPE_API_TOKEN`, require loopback, print only the selected port, and pass a synthetic token from `dev.js` through the environment.

**Step 4: Run the focused GREEN gate**

Run: `python -m unittest qa.tests.test_security_contract -v`

Expected: PASS.

**Step 5: Static secret/output audit and commit**

Run: `rg -n "allow_origins=\[\"\*\"\]|allow_methods=\[\"\*\"\]|allow_headers=\[\"\*\"\]|DECKPIPE_API_TOKEN.*print|print\(.*token" app run_backend.py dev.js qa`

Expected: no unsafe match.

Commit: `feat(security): enforce authenticated loopback API`

### Task 2A: Create one CSP-compatible frontend build

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/app.js`
- Create: `frontend/styles.css`
- Create: `frontend/build.mjs`
- Modify: `package.json`
- Create: `package-lock.json`
- Generate: `app/static/index.html`
- Generate: `app/static/app.js`
- Generate: `app/static/styles.css`
- Generate: `desktop/ui/index.html`
- Generate: `desktop/ui/app.js`
- Generate: `desktop/ui/styles.css`
- Create: `qa/tests/test_frontend_build_contract.py`

**Step 1: Write failing frontend-build tests**

Assert: `frontend/` is the only editable source; a clean build emits byte-identical assets to `app/static/` and `desktop/ui/`; JavaScript imports `invoke` from `@tauri-apps/api/core` instead of using `window.__TAURI__`; HTML contains no inline scripts, styles, event handlers, or `javascript:` URLs; generated assets contain no source-map path disclosure; the current user-visible controls and API actions remain present.

**Step 2: Run RED**

Run: `python -m unittest qa.tests.test_frontend_build_contract -v`

Expected: FAIL because the real UI is one inline FastAPI document while the desktop target is a placeholder.

**Step 3: Extract and mechanically preserve UI behavior**

Split the existing document into canonical HTML/CSS/JavaScript. Replace static and generated inline event attributes with delegated listeners and fixed `data-action` values; replace inline style attributes with named classes. Introduce one API wrapper whose release path obtains `{baseUrl, token}` through a narrow Tauri `backend_connection` command and caches it in memory only. Browser development may use an explicit synthetic test injection, but release assets must not persist or render the token. Keep deeper untrusted-HTML removal, accessibility semantics, responsive polish, and honest error-state redesign for Task 6.

**Step 4: Run GREEN and deterministic-build gates**

Run: `npm ci && npm run build:frontend && python -m unittest qa.tests.test_frontend_build_contract -v`

Run the build a second time in a temporary output directory and compare hashes; do not modify runtime outputs during the comparison.

Expected: tests pass and both runtime targets are identical.

**Step 5: Commit**

Commit: `build(frontend): create canonical CSP-safe assets`

### Task 2B: Bundle the Tauri window and own the sidecar lifecycle

**Files:**
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `desktop/src-tauri/Cargo.toml`
- Modify: `desktop/src-tauri/Cargo.lock`
- Modify: `desktop/src-tauri/src/main.rs`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `desktop/src-tauri/capabilities/default.json`
- Modify: `desktop/src-tauri/permissions/service-login.toml`
- Create: `desktop/src-tauri/permissions/backend-connection.toml`
- Modify: `run_backend.py`
- Modify: `dev.js`
- Create: `qa/tests/test_desktop_contract.py`

**Step 1: Write failing desktop contract tests**

Assert: `withGlobalTauri` is false; CSP has no `unsafe-inline`/`unsafe-eval`; no external main-window URL or remote capability scope; IPC permits only login and read-once/in-memory backend connection data to the `main` window; the Rust launcher creates a random 32-byte token, passes token and parent PID through environment, retains the child, kills it on exit/error, and enforces one desktop instance. The backend must bind the actual listening socket before publishing `DECKPIPE_PORT`, use that same socket for Uvicorn, and exit when its parent dies.

**Step 2: Run RED**

Run: `python -m unittest qa.tests.test_desktop_contract -v`

Expected: FAIL against the external localhost window, dropped child handle, and close-then-rebind free-port probe.

**Step 3: Build the bundled architecture**

Load `WebviewUrl::App` from the Task 2A assets. Add strict CSP and security headers. Generate a 32-byte token with OS randomness, store connection data in Tauri managed state, expose it only through the narrow main-window command, pass token plus `DECKPIPE_PARENT_PID` through sidecar environment, retain the child in managed state, terminate it during teardown and failed startup, and add single-instance behavior. Replace port probing with one pre-bound listening socket handed directly to Uvicorn; print the selected port only after successful bind and never close/rebind it.

**Step 4: Run GREEN and compiler gates**

Run: `npm ci && npm run build:frontend && python -m unittest qa.tests.test_desktop_contract -v`

Run: `cargo check --locked`

Expected: all pass; no new Rust warnings in changed code.

**Step 5: Commit**

Commit: `feat(desktop): bundle UI and supervise sidecar`

### Task 3: Move credentials to an isolated Windows secure store and retire the unsafe extension bridge

**Files:**
- Create: `app/secure_store.py`
- Modify: `app/deezer_client.py`
- Modify: `app/soundcloud.py`
- Modify: `app/main.py`
- Modify: `extension/manifest.json`
- Modify: `extension/popup.html`
- Modify: `extension/popup.js`
- Delete: `extension/background.js`
- Create: `qa/tests/test_secure_store.py`
- Create: `qa/tests/test_extension_contract.py`

**Step 1: Write failing tests with synthetic secrets**

Use temporary application directories only. Verify Windows DPAPI encrypt/decrypt uses current-user scope and UI-forbidden flags; JSON configuration contains no credential material; legacy migration writes the secure blob, verifies a read-back, then atomically removes only migrated secret fields; interrupted migration leaves the legacy source intact; logs/reports never contain the synthetic values. Assert the extension requests neither `cookies` nor localhost host permissions, contains no port scan, and directs users to the desktop login flow.

**Step 2: Run RED**

Run: `python -m unittest qa.tests.test_secure_store qa.tests.test_extension_contract -v`

Expected: FAIL because plaintext configuration and cookie bridge remain.

**Step 3: Implement the secure-store boundary**

Wrap `CryptProtectData`/`CryptUnprotectData` with explicit memory cleanup and no UI. Separate non-secret preferences from secret records. Make migration an explicit callable startup step that is idempotent and crash-safe; do not execute it during tests against the real profile. Remove extension cookie/localhost/background permissions and replace the popup with a safe deprecation/desktop-login message. Do not implement a weaker HTTP bridge.

**Step 4: Run GREEN and repository secret scan**

Run: `python -m unittest qa.tests.test_secure_store qa.tests.test_extension_contract -v`

Run the repository's secret scan against tracked changes and generated reports.

Expected: tests pass and no synthetic or live credential-shaped values appear.

**Step 5: Commit**

Commit: `feat(secrets): add DPAPI store and disable cookie bridge`

### Task 4: Make media, state, and jobs crash-safe and indexed

**Files:**
- Create: `app/atomic_io.py`
- Modify: `app/library.py`
- Modify: `app/jobs.py`
- Modify: `app/deezer_client.py`
- Modify: `app/soundcloud.py`
- Modify: `app/converter.py`
- Modify: `app/main.py`
- Modify: `qa/tests/test_isolated_core.py`
- Create: `qa/tests/test_crash_safety.py`
- Modify: `qa/benchmark_library_scan.py`
- Modify: `qa/performance-budget.json`

**Step 1: Write failing crash and complexity tests**

Cover: per-library lock; same-directory temporary write plus flush/fsync and atomic replace; recovery from corrupt/truncated sidecar; download/convert publication only after validation; no `.part` file reported as ready; durable job journal with terminal error details; restart resumes only idempotent work; one prebuilt `(provider, id)` index per scan rather than per-track recursive traversal; cold and warm performance budgets.

**Step 2: Run RED**

Run: `python -m unittest qa.tests.test_crash_safety qa.tests.test_isolated_core -v`

Run: `python qa/benchmark_library_scan.py`

Expected: correctness tests fail and the pathological scan exceeds budget.

**Step 3: Implement atomic publication and indexed state**

Centralize lock acquisition, atomic JSON, and atomic file publication. Keep recoverable `.bak` state without ever placing credentials there. Make job transitions monotonic and persist them once per logical transition. Build the library index once, normalize paths once, and update it incrementally after successful publication.

**Step 4: Run GREEN**

Run the focused unittest set and benchmark once.

Expected: all correctness cases pass and both performance budgets are met.

**Step 5: Commit**

Commit: `feat(state): add crash-safe jobs and indexed library`

### Task 5: Add a transactional, reconciled Rekordbox adapter

**Files:**
- Rewrite: `app/rekordbox.py`
- Modify: `app/jobs.py`
- Modify: `app/main.py`
- Create: `qa/tests/test_rekordbox_sync.py`
- Modify: `qa/run-rekordbox-unicode-audit.ps1`

**Step 1: Write failing adapter tests**

Define the metadata boundary `{provider_id,title,artist,album,duration,position,path}`. With a fake adapter, test deterministic dry-run diffs for add/remove/reorder/metadata/path/unresolved; unique backup names; exclusive mutation lock; DB plus affected ANLZ backup inventory; one logical transaction; rollback on any adapter/ANLZ/reopen/reconcile failure; exact reopened order and metadata reconciliation; sidecar advancement only after successful reconcile. Verify all live writes require both `DECKPIPE_RB_EXPERIMENTAL=1` and an explicit API confirmation token.

**Step 2: Run RED**

Run: `python -m unittest qa.tests.test_rekordbox_sync -v`

Expected: FAIL against append-only sync and false-success flip behavior.

**Step 3: Implement plan/apply/reconcile**

Separate pure planning from the pyrekordbox adapter. Never use helper methods that commit internally inside the logical transaction. Inventory and copy every externally written ANLZ file before mutation. On failure, close handles, restore DB and ANLZ files, reopen, and verify the restored snapshot. Return structured unresolved items and never claim success from an affected-row count alone. Keep real apply fail closed by default.

**Step 4: Run GREEN and disposable-copy gate**

Run the fake-adapter unittest suite. If and only if an explicit disposable Rekordbox copy is supplied, run the Unicode/integration audit against that copy; otherwise record the RC gate as blocked, not passed.

**Step 5: Commit**

Commit: `feat(rekordbox): plan apply and reconcile sync`

### Task 6: Repair responsive layout, keyboard access, and honest error states

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/app.js`
- Modify: `frontend/styles.css`
- Regenerate: `app/static/*`
- Regenerate: `desktop/ui/*`
- Create: `qa/tests/test_frontend_contract.py`
- Modify: `qa/run-installed-qa.ps1`

**Step 1: Write failing static and behavior tests**

Assert no inline event handlers or HTML-string interpolation of untrusted values; all dynamic content uses text nodes or fixed templates; all controls are reachable and operable by keyboard; focus is visible; dialogs trap/restore focus; errors include local/security/state/Rekordbox failures; minimum supported window has no clipped critical action; reduced-motion and accessible names are present.

**Step 2: Run RED**

Run: `python -m unittest qa.tests.test_frontend_contract -v`

Expected: FAIL on inline handlers, unsafe HTML sinks, and missing accessibility contracts.

**Step 3: Refactor the canonical frontend**

Use delegated event listeners and safe DOM construction, responsive grid/flex breakpoints, semantic controls, live regions, and a single API wrapper that injects the in-memory launch token without persisting it. Present dry-run/apply/reconcile and local failure states truthfully. Rebuild both targets.

**Step 4: Run GREEN and installed UI gate once**

Run static tests and the focused installed UI harness at minimum supported size. Capture evidence without credentials or user-library content.

**Step 5: Commit**

Commit: `fix(ui): harden rendering and accessibility`

### Task 7: Lock version, dependencies, and Windows release evidence

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.in`
- Create: `requirements.lock`
- Create: `requirements-build.lock`
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `desktop/src-tauri/Cargo.toml`
- Modify: `desktop/src-tauri/Cargo.lock`
- Modify: `desktop/src-tauri/tauri.conf.json`
- Modify: `app/main.py`
- Modify: `app/bugreport.py`
- Modify: `qa/performance-budget.json`
- Create: `release/version.json`
- Create: `release/build.ps1`
- Create: `release/verify.ps1`
- Create: `release/New-SpdxSbom.ps1`
- Create: `release/README.md`
- Create: `qa/tests/DeckPipe.Release.Tests.ps1`

**Step 1: Write failing release-contract tests**

Assert every version source derives from `release/version.json` and resolves to 0.6.0 plus a build ID; Python/npm/Cargo installs are locked; clean builds reject drift; artifact names include version/build; verification requires a valid timestamped Authenticode signature, SHA-256 manifest, SPDX 2.3 SBOM, and no forbidden credential/config files; scripts are PowerShell 5.1-compatible and never discover certificates by dumping stores.

**Step 2: Run RED**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1`

Expected: FAIL on version drift and missing pipeline.

**Step 3: Implement reproducible release tooling**

Pin direct and transitive Python build/runtime dependencies with hashes, preserve npm and Cargo locks, synchronize metadata from the canonical version file, build in a clean staging directory, generate checksum and SPDX SBOM deterministically, and verify the staged allowlist. Accept a signing certificate selector and timestamp URL only as explicit inputs; fail closed when unavailable. Never sign with or inspect unrelated user certificates.

**Step 4: Run GREEN and engineering-candidate build**

Run release-contract tests, then build an unsigned engineering candidate only if the locked toolchain is available. The verifier must mark signing/timestamp as BLOCKED, never PASS, until authorized credentials are supplied.

**Step 5: Commit**

Commit: `build(release): lock 0.6.0 Windows pipeline`

### Task 8: Run combined verification, security review, and RC evidence gates

**Files:**
- Create: `audit/deckpipe-release-hardening-verification-2026-08-27.md`
- Create or update: `.superpowers/sdd/deckpipe-release-hardening-handoff-2026-08-27/progress.md` (ignored working ledger)
- Modify only if required by the bounded fix round: files named by reviewer findings

**Step 1: Run the combined suite once**

Run existing PowerShell QA tests, all Python unittests, `compileall`, frontend build, JS syntax checks, `cargo check --locked`, dependency integrity checks, and `git diff --check`. Record exact commands, exit codes, and artifact hashes.

**Step 2: Run security gates**

Run standard and diff-focused repository scans plus one deep release-candidate scan. Re-run hostile-origin/auth/XSS/extension reproducers against the installed candidate. Treat any P0/P1 or credential-bearing artifact as a release blocker.

**Step 3: Request one whole-branch independent review**

Reviewer compares `b6b3eb4..HEAD` to this plan and the handoff, checks threat boundaries, rollback paths, artifact allowlist, tests, and UX truthfulness. Findings must include file/line evidence and severity.

**Step 4: Apply at most one bounded fix round**

Fix only validated P0/P1 or release-contract failures, rerun the smallest affected tests, then rerun the combined suite only if a shared contract changed.

**Step 5: Execute RC-only gates once**

On the installed EXE and a clean Windows profile/machine, verify first run, upgrade from the preserved 0.5.0 artifact, uninstall cleanup, no orphan process, real network behavior, and a disposable copy of the real Rekordbox library. Inspect Authenticode timestamp, SHA-256 manifest, SPDX SBOM, and package contents. If any external prerequisite is unavailable, mark the named gate BLOCKED with the exact missing authority/input.

**Step 6: Final decision**

Only label the candidate releasable when every mandatory gate is PASS. Otherwise report the engineering work complete with an explicit release-blocker list; do not publish artifacts.

## Owner-approved private-beta RC closure addendum — 2026-08-28

**Status:** Approved for execution. This addendum implements
`docs/superpowers/specs/2026-08-28-deckpipe-private-beta-rc-closure-design.md`
and supersedes the earlier assumptions below only for future RC work. It does
not rewrite the 2026-08-27 audit or turn any historical `BLOCKED` result into a
pass.

| Earlier assumption | Approved rule for this closure |
|---|---|
| Lines 17 and 31 prohibit live Rekordbox use | The current Rekordbox library may be backed up, tested, and mutated without another approval. The product's process-closed, backup, transaction, reconcile, integrity, and restore gates remain mandatory. |
| Lines 18, 24, 32, 328, 338, 342, and 373 require Authenticode and RFC3161 evidence | The supported output is an **unsigned private-beta** candidate. Signing and timestamping are recorded as `WAIVED_BY_OWNER`, never `PASS`. A future public release still requires both. |
| Task 8 assumes a separate disposable Rekordbox copy | Start with a D-drive backup of the live library, execute the gated live test, and restore that backup after the test playlist has been reconciled. |
| Task 8 assumes a clean VM/machine | Use a dedicated standard local Windows user, preferably with a non-ASCII name, on this host. A VM is optional and Docker is not useful for native installer, WebView2, logout/login, or Rekordbox acceptance. |
| Task 8 assumes test accounts | Use the owner's Deezer and SoundCloud accounts through DeckPipe's interactive login window. Never export browser cookies or tokens. Pause only when the owner must complete login/MFA. |
| Official deep scanner is a hard prerequisite | Run it only if a scanner is callable in the session. Otherwise record `WAIVED_BY_OWNER`; the existing hostile tests and a manual security review still must pass. |

All explicitly chosen downloads, caches, toolchains, build outputs, installers,
backups, and evidence belong below `D:\DeckPipe-RC-Lab`. Windows may still make
unavoidable OS-managed writes on `C:` for profiles, registry, drivers, WebView2,
installer metadata, and temporary system components. Do not deliberately choose
`C:` for a payload or cache. Do not touch the dirty main checkout at
`D:\Claude Code\Projects\deezer-rekordbox-sync`; execute only in the clean
`release-hardening` worktree.

### Task 9: Encode the private-beta waiver as a fail-closed release contract

**Files:**

- Create: `release/policy.json`
- Modify: `release/build.ps1`
- Modify: `release/verify.ps1`
- Modify: `release/README.md`
- Modify: `qa/run-installed-qa.ps1`
- Modify: `qa/tests/DeckPipe.Release.Tests.ps1`
- Modify: `qa/tests/test_installed_runner_contract.py`

- [ ] **Step 1: Add RED tests for one exact tracked policy**

  Require the tracked file to have exactly this semantic payload (normal JSON
  whitespace is immaterial, extra properties are rejected):

  ```json
  {
    "schema_version": 1,
    "channel": "private-beta",
    "signing_requirement": "owner-waived",
    "timestamp_requirement": "owner-waived",
    "windows_reputation_warning": "accepted",
    "waiver_date": "2026-08-28",
    "intended_audience": "controlled-small-group"
  }
  ```

  Add release tests proving that a candidate can reach overall `PASS` without
  Authenticode only when the staged `policy.json` is byte-for-byte identical to
  the tracked policy, its SHA-256 is bound into `release-evidence.json`, and all
  non-waived checks pass. Add hostile cases for a missing/extra policy field,
  `public` channel, caller-supplied policy path, forged policy hash, `PASS`
  signing status without a valid signature, and `WAIVED_BY_OWNER` without the
  exact private-beta policy.

- [ ] **Step 2: Add RED installed-runner tests**

  Preserve `-AllowUnsignedEngineeringEvidence` only for explicit engineering
  diagnostics. Prove that normal installed QA accepts a verifier-confirmed
  private-beta `PASS`, that legacy unsigned-engineering evidence still cannot
  aggregate to `PASS`, and that neither a parent-scope function nor a fake
  verifier/policy path can authorize launch.

  Run:

  ```powershell
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1
  rtk proxy ..\..\.venv\Scripts\python.exe -m unittest qa.tests.test_installed_runner_contract -v
  ```

  Expected: the new cases fail before implementation; existing signature and
  engineering-candidate cases remain green.

- [ ] **Step 3: Implement the policy and evidence schema**

  Add `-PrivateBetaCandidate` to `release/build.ps1`; reject combining it with
  `-UnsignedEngineeringCandidate` or any signing inputs. Read policy only from
  the tracked `release/policy.json`, copy it into staging, and include it in the
  manifest, SBOM, allowlist, and evidence. For this mode write:

  The evidence contract is:

  - `distribution.channel` = `private-beta`;
  - `distribution.artifact_label` = `unsigned-private-beta`;
  - `distribution.policy_path` = `policy.json`;
  - `distribution.policy_sha256` = the computed lowercase digest matching
    `^[0-9a-f]{64}$`;
  - `signing.status` = `WAIVED_BY_OWNER`, `signing.signed` = an empty array,
    and `signing.policy_path` = `policy.json`;
  - `timestamp.status` = `WAIVED_BY_OWNER` and `timestamp.policy_path` =
    `policy.json`.

  Keep the existing signed path strict for a future public policy and the
  existing unsigned-engineering path `BLOCKED`. Add
  `-unsigned-private-beta` to every private-beta artifact basename so the user
  cannot mistake it for a signed/public build.

- [ ] **Step 4: Make the verifier authoritative**

  `release/verify.ps1` must load the tracked policy through `$PSScriptRoot`,
  never a caller argument; compare the staged policy, distribution label, and
  evidence hash; validate every manifest/SBOM/allowlist/provenance rule; and
  return the existing JSON/exit contract (`PASS`/0, `FAIL`/1, `BLOCKED`/2).
  `WAIVED_BY_OWNER` is valid only for the exact private-beta policy. Any public
  or absent policy restores the valid Authenticode plus timestamp requirement.

- [ ] **Step 5: Run GREEN and commit**

  Run the two commands from Steps 1–2 plus `git diff --check`. Expected: all
  tests pass; no real artifact is built yet.

  Commit: `build(release): encode private beta waiver policy`

### Task 10: Close hash locks and construct the D-drive offline wheelhouse

**Files:**

- Create: `requirements-build.in`
- Create: `release/prepare-wheelhouse.ps1`
- Modify: `requirements.lock`
- Modify: `requirements-build.lock`
- Modify: `release/build.ps1`
- Modify: `release/README.md`
- Modify: `qa/tests/DeckPipe.Release.Tests.ps1`

- [ ] **Step 1: Create and verify the lab boundary**

  Use this fixed layout and no alternate payload/cache roots:

  ```powershell
  $LabRoot = 'D:\DeckPipe-RC-Lab'
  $LabDirectories = @(
    'downloads', 'wheelhouse', 'tool-cache', 'build', 'staging', 'install',
    'backups\rekordbox', 'qa-evidence', 'logs-redacted'
  )
  foreach ($relative in $LabDirectories) {
    New-Item -ItemType Directory -Force -Path (Join-Path $LabRoot $relative) | Out-Null
  }
  ```

  Record the Windows edition/build, free D: space, Python 3.12 x64 path, Node,
  npm, Rust, Cargo, PowerShell 5.1, WebView2, and Rekordbox inventory in a
  redacted JSON file under `qa-evidence`. If a required build tool is missing,
  install its payload/cache under `tool-cache`; accept only unavoidable
  Windows-managed writes on `C:`.

- [ ] **Step 2: Add RED lock/wheelhouse contract tests**

  Require both lock files to contain `--require-hashes`, a real SHA-256 for
  every resolved requirement, no `LOCK-STATUS: BLOCKED`, and no unpinned URL or
  editable requirement. Require `wheelhouse-manifest.json` to inventory every
  offline file by relative name, size, and lowercase SHA-256. Prove the build
  rejects a missing, extra, renamed, or hash-mismatched wheel and any wheelhouse
  inside the repository or staging directory.

  Run:

  ```powershell
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1
  ```

  Expected: fail on the two blocked locks and absent wheelhouse contract.

- [ ] **Step 3: Implement deterministic lock generation**

  `requirements-build.in` contains only the direct build roots already intended
  by the blocked lock: `pip==26.2`, `setuptools==83.0.0`, and
  `pyinstaller==6.21.0`. `release/prepare-wheelhouse.ps1` accepts mandatory
  `-LabRoot`, `-PythonExe`, and `-PipToolsVersion`; refuses a root outside
  `D:\DeckPipe-RC-Lab`; uses a tool venv below `tool-cache`; compiles Windows
  CPython 3.12 locks with hashes; downloads binary wheels; writes the sorted
  manifest; and proves an offline install in a newly created D-drive venv.
  Pin the generator invocation to `pip-tools==7.6.1`; do not add pip-tools to
  the shipped runtime/build locks.

  Run:

  ```powershell
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File release/prepare-wheelhouse.ps1 `
    -LabRoot 'D:\DeckPipe-RC-Lab' `
    -PythonExe '..\..\.venv\Scripts\python.exe' `
    -PipToolsVersion '7.6.1'
  ```

  The script itself must execute the equivalent of
  `pip-compile --generate-hashes --allow-unsafe` for both `.in` files, `pip download
  --require-hashes --only-binary=:all:`, then `pip install --no-index
  --require-hashes` from only the resulting wheelhouse. If any dependency lacks
  a compatible Windows x64 CPython 3.12 wheel, stop with the exact package name;
  do not silently build an untracked sdist.

- [ ] **Step 4: Bind the build to the proven wheelhouse**

  `release/build.ps1` must verify the manifest before creating staging, install
  runtime/build locks with `--no-index --find-links --require-hashes`, and copy
  the wheelhouse manifest hash into release evidence. It must not read the
  user's global pip cache or synthesize a hash.

- [ ] **Step 5: Run GREEN and commit**

  Re-run the release tests, repeat the offline install in a fresh D-drive venv,
  run `pip check`, and run `git diff --check`. Do not commit wheel files, venvs,
  caches, or external evidence.

  Commit: `build(deps): lock Windows wheelhouse with hashes`

### Task 11: Build and freeze one exact native unsigned private-beta candidate

**Files:**

- Modify only if a failing native build exposes a validated release-contract bug:
  `release/build.ps1`, `release/verify.ps1`, `release/New-SpdxSbom.ps1`,
  `desktop/src-tauri/*`, or their directly affected tests
- Create externally: `D:\DeckPipe-RC-Lab\qa-evidence\candidate-pin.json`

- [ ] **Step 1: Make the native toolchain use D-drive caches**

  In the build shell set `CARGO_HOME`, `RUSTUP_HOME`, `npm_config_cache`,
  `PIP_CACHE_DIR`, `TEMP`, and `TMP` to subdirectories of
  `D:\DeckPipe-RC-Lab\tool-cache`. Re-run `npm ci`, `npm --prefix desktop ci`,
  and `cargo fetch --locked` only as required. Do not use Docker: it cannot prove
  the Windows Tauri/NSIS/MSI/WebView2 lifecycle.

- [ ] **Step 2: Prove the tracked-source precondition**

  Require the branch worktree to be clean, record full lowercase `HEAD`, and
  confirm the main checkout has not been touched. The build must use its
  isolated tracked-HEAD workspace and fail if source, locks, policy, or
  wheelhouse drift after preflight.

- [ ] **Step 3: Create a fresh bounded staging directory**

  Resolve `D:\DeckPipe-RC-Lab\staging\deckpipe-0.6.0-private-beta` to an
  absolute path and verify it starts with
  `D:\DeckPipe-RC-Lab\staging\` before removing an earlier failed attempt.
  Create an empty directory and run:

  ```powershell
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File release/build.ps1 `
    -PrivateBetaCandidate `
    -StagingDirectory 'D:\DeckPipe-RC-Lab\staging\deckpipe-0.6.0-private-beta' `
    -PythonExe 'D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe' `
    -WheelhouseDirectory 'D:\DeckPipe-RC-Lab\wheelhouse'
  ```

  Do not pass SignTool, certificate, or timestamp arguments.

- [ ] **Step 4: Verify and freeze candidate identity**

  Run `release/verify.ps1` against staging and require JSON `PASS` with exit 0.
  Confirm all top-level installer/portable filenames contain
  `unsigned-private-beta`; top-level Authenticode status is `NotSigned` and is
  reported as waived, not passed; SHA-256 sums match; SPDX 2.3 relationships
  are exact; package allowlist contains no credential, browser-profile,
  Rekordbox, database, media, or local-config file. Write the source revision,
  build ID, artifact names/sizes/hashes, evidence hash, policy hash, SBOM hash,
  and wheelhouse-manifest hash to `candidate-pin.json`. Never modify staging
  after this point.

- [ ] **Step 5: Run release tests and commit only validated fixes**

  If native build work required a source fix, first add a failing test, make the
  smallest fix, rerun the affected test and full release tests, and commit:
  `fix(release): close native private beta build blocker`. Otherwise make no
  empty commit.

### Task 12: Prove install, upgrade/migration, logout/login, and uninstall on a clean Unicode profile

**Files:**

- Modify only for a reproduced installed-lifecycle defect:
  `qa/run-installed-qa.ps1`, `qa/tests/test_installed_runner_contract.py`, and
  the directly failing product file
- Create externally: evidence below
  `D:\DeckPipe-RC-Lab\qa-evidence\installed-profile`

- [ ] **Step 1: Rehash before every install**

  Compare staging to `candidate-pin.json`. Any mismatch invalidates all later
  evidence and requires rebuilding from a clean tracked commit.

- [ ] **Step 2: Create a dedicated standard local test user**

  Use a unique non-ASCII name such as `DeckPipe_Тест_0828`; refuse to reuse or
  overwrite an existing account. Create it as a standard user with a password
  entered through `Read-Host -AsSecureString`, not a command-line/log value.
  Its Windows-managed profile may be on `C:`; installers, app payloads, test
  media, and QA output stay on `D:`. Record only the username label and profile
  path, never the password.

- [ ] **Step 3: Checkpoint before logout or reboot**

  Commit all source changes and write
  `D:\DeckPipe-RC-Lab\qa-evidence\resume-after-login.md` with the exact
  candidate hashes, completed gates, next command, and expected account. Tell
  the owner which gate requires logout/login. Do not claim the session will
  resume automatically; after the owner logs in, they must reopen/continue the
  Codex task unless a real scheduled mechanism exists.

- [ ] **Step 4: Exercise the actual packaging lifecycle**

  Under the clean user, install to
  `D:\DeckPipe-RC-Lab\install\DeckPipe-0.6.0-private-beta`, acknowledge the
  expected unknown-publisher warning, and test first launch, backend readiness,
  packaged-origin authentication, restart, logout/login persistence,
  crash/restart recovery, reinstall, uninstall cleanup, and sidecar/orphan
  termination. Run normal installed QA without the engineering escape hatch:

  ```powershell
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/run-installed-qa.ps1 `
    -ExePath 'D:\DeckPipe-RC-Lab\install\DeckPipe-0.6.0-private-beta\DeckPipe.exe' `
    -CandidateEvidenceDirectory 'D:\DeckPipe-RC-Lab\staging\deckpipe-0.6.0-private-beta' `
    -OutputDirectory 'D:\DeckPipe-RC-Lab\qa-evidence\installed-profile' `
    -Samples 5
  ```

  Also rerun installed hostile-origin/auth/XSS/extension reproducers and the
  performance budget against this exact process tree.

- [ ] **Step 5: Test the real 0.5.0 migration shape**

  Revalidate `E:\DeckPipe\deckpipe.exe`, copy it without modifying the source
  into `D:\DeckPipe-RC-Lab\install\preserved-0.5.0`, and record its hash. If it
  is portable, test preserved user-state migration into 0.6.0 and explicitly
  record `MSI upgrade: NOT_APPLICABLE`; do not fabricate an installer upgrade.
  If an actual 0.5.0 installer is found, add a real same-format upgrade test.

- [ ] **Step 6: Close the lifecycle**

  Verify uninstall leaves no owned process/listener and no installed payload
  outside the expected Windows installer metadata, reinstall for integration
  tasks, and rehash the original candidate. Disable the test account after all
  clean-profile gates; do not delete its profile automatically.

### Task 13: Run redacted live Deezer and SoundCloud acceptance

**Files:**

- Create: `audit/deckpipe-private-beta-live-accounts-2026-08-28.md`
- Modify only for a reproduced integration defect: the directly affected
  provider/UI file plus its existing contract test

- [ ] **Step 1: Prove login-cancel behavior before adding credentials**

  On the clean profile, open and close one DeckPipe provider-login window and
  require the neutral `DECKPIPE_LOGIN_CANCELLED` UX. Confirm no token appears in
  stdout/stderr, app logs, bug reports, QA JSON, command history, or tracked
  files.

- [ ] **Step 2: Let the owner complete interactive authentication**

  Open DeckPipe's own Deezer/SoundCloud login windows. If either service asks
  for credentials or MFA, stop at that visible window and ask the owner to
  complete it. Never read/export the already logged-in browser's cookies,
  profile, credential database, ARL, OAuth token, or refresh token. DeckPipe may
  capture its own login-window cookie into the current-user DPAPI store as
  designed.

- [ ] **Step 3: Exercise useful bounded flows**

  For Deezer: load the account playlist list, open one playlist, run a search,
  and download one selected sample into the D-drive lab. For SoundCloud: load
  account collections, resolve/import one source, run a search, and download
  one selected sample. Verify publication, metadata/tagging, restart recovery,
  and a generic invalid-URL/network-error path. Do not create purchases,
  subscriptions, security changes, bulk downloads, remote deletions, or remote
  playlists that the product cannot clean up.

- [ ] **Step 4: Verify DPAPI persistence and redaction**

  Restart and perform the approved logout/login checkpoint; confirm both
  sessions remain available only to the same Windows user. Scan outputs for
  credential field names and sentinel patterns without printing matched secret
  values. The tracked account audit records only provider, flow name,
  PASS/FAIL/BLOCKED, aggregate counts, candidate hash, and redacted error class;
  it contains no account names, playlist/track titles, URLs, IDs, local user
  paths, or token-shaped values. Remove lab media after recording non-content
  evidence and verifying the exact D-drive target.

- [ ] **Step 5: Fix only reproduced defects**

  For each failure, first add the smallest offline regression test, then patch
  the direct integration surface and rerun its suite plus installed QA. Do not
  weaken auth/origin/CSP/DPAPI controls to accommodate a provider.

### Task 14: Run authorized live Rekordbox backup/apply/reconcile/restore acceptance

**Files:**

- Create: `qa/run-rekordbox-live-acceptance.ps1`
- Modify: `app/rekordbox.py`
- Modify: `qa/tests/test_rekordbox_sync.py`
- Modify: `qa/tests/DeckPipe.Orchestrator.Tests.ps1`
- Create: `audit/deckpipe-private-beta-rekordbox-acceptance-2026-08-28.md`

- [ ] **Step 1: Add RED tests for an explicit external backup root and live runner**

  Require an explicit backup root for the RC live run, refuse a reparse point or
  a path outside `D:\DeckPipe-RC-Lab\backups\rekordbox`, refuse mutation while
  Rekordbox is running, and preserve the existing environment flag plus exact
  `APPLY_REKORDBOX_CHANGES` confirmation. Test inventory/hash capture, dry-run
  zero mutation, apply/reopen/reconcile, rollback on every failure surface, and
  final restore matching the original snapshot. Keep
  `qa/run-rekordbox-unicode-audit.ps1` read-only and explicit-path only.

  Run:

  ```powershell
  rtk proxy ..\..\.venv\Scripts\python.exe -m unittest qa.tests.test_rekordbox_sync -v
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Orchestrator.Tests.ps1
  ```

- [ ] **Step 2: Implement the bounded live runner**

  `qa/run-rekordbox-live-acceptance.ps1` accepts mandatory `-DatabasePath`,
  `-BackupRoot`, `-EvidenceDirectory`, `-PythonPath`, `-PlaylistPrefix`, and an
  explicit `-Apply` switch plus `-ConfirmationToken`. It must inventory every
  adapter-declared database/ANLZ/XML/external file, copy and SHA-256 it before
  opening a write handle, perform a read-only integrity/snapshot check, emit the
  proposed dry-run operation counts, and only then allow apply. Evidence is
  aggregate/redacted and binds the installed candidate hash.

- [ ] **Step 3: Revalidate the live target immediately before mutation**

  Confirm Rekordbox 7.2.14 at
  `F:\rekordbox 7.2.14\rekordbox.exe`, database
  `C:\Users\peche\AppData\Roaming\Pioneer\rekordbox\master.db`, and zero
  Rekordbox processes. Recompute size/time/hash; do not rely on the 2026-08-28
  inventory. If Rekordbox is running, close it normally and recheck before any
  copy or write.

- [ ] **Step 4: Back up, audit Unicode, and execute the gated test write**

  First run the existing Unicode audit with the live database as a read-only
  source and a D-drive output. Then run the new live runner with a generated
  unique prefix beginning `DECKPIPE_RC_20260828_`,
  `DECKPIPE_RB_EXPERIMENTAL=1`, `-Apply`, and the exact confirmation token.
  Create/use the test playlist, apply a minimal change using existing library
  tracks, close/reopen the adapter, and require exact order/metadata/path
  reconcile plus integrity PASS.

- [ ] **Step 5: Restore and prove no residue**

  Close all adapter handles, restore the pretest D-drive backup, reopen and
  compare the full inventory plus hashes to the original snapshot, and confirm
  the test playlist is absent. If restore verification fails, stop using
  Rekordbox and preserve both copies for diagnosis; the owner has accepted that
  the library can be recreated, but that is not permission to hide a failed
  restore.

- [ ] **Step 6: Run GREEN and commit**

  Re-run the two suites from Step 1 and commit implementation plus redacted
  acceptance evidence only after restore verification:
  `test(rekordbox): prove authorized live rc transaction`.

### Task 15: Re-run the whole branch, review it independently, and issue the final RC decision

**Files:**

- Create: `audit/deckpipe-private-beta-rc-verification-2026-08-28.md`
- Modify only for one bounded validated fix round: files named by P0/P1 or
  release-contract findings and their tests

- [ ] **Step 1: Run the complete source and build matrix from a clean tree**

  Use the D-drive offline-proof Python 3.12 environment and run:

  ```powershell
  rtk proxy D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe -m unittest discover -s qa\tests -p 'test_*.py'
  rtk proxy D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe -m compileall app qa
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.QA.Tests.ps1
  rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Orchestrator.Tests.ps1
  rtk npm run build:frontend
  rtk npm ls --all --offline
  rtk npm --prefix desktop ls --all --offline
  rtk proxy D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe -m pip check
  rtk proxy D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe qa/benchmark_library_scan.py
  rtk cargo check --manifest-path desktop/src-tauri/Cargo.toml --locked --offline
  rtk cargo test --manifest-path desktop/src-tauri/Cargo.toml --locked --offline
  rtk git diff --check b6b3eb4..HEAD
  rtk git diff --check
  ```

  Record exact commands, exit codes, test counts, and only redacted failure
  summaries. Re-run `release/verify.ps1`, installed QA, account acceptance, and
  Rekordbox acceptance against the frozen candidate hash rather than rebuilding
  opportunistically.

- [ ] **Step 2: Complete security closure**

  Re-run source and installed hostile auth/origin/CSP/XSS/extension/DPAPI
  regressions; inspect the staged allowlist and logs for credential-bearing
  data without printing secret contents. If an official Codex Security scanner
  is callable, run standard, diff, and candidate scans. If it is not callable,
  record `WAIVED_BY_OWNER` with the local-test/manual-review evidence; do not
  install or claim a fake scanner result.

- [ ] **Step 3: Request one whole-branch read-only review**

  Review `b6b3eb4..HEAD` against the approved design, this addendum, and the
  candidate evidence. Require file/line evidence, severity, and explicit checks
  of waiver non-escalation, lock/wheelhouse integrity, source provenance,
  installed process ownership, DPAPI/log redaction, and Rekordbox restore. The
  reviewer must not edit or commit. Apply at most one bounded P0/P1 or
  release-contract fix round, with RED/GREEN evidence, then rerun every affected
  shared gate.

- [ ] **Step 4: Write the final audit without rewriting history**

  The new audit must keep the 2026-08-27 report historical and include one row
  per gate with `PASS`, `FAIL`, `BLOCKED`, `WAIVED_BY_OWNER`, or
  `NOT_APPLICABLE`; exact source and candidate hashes; clean-profile and live
  integration evidence paths; known Windows unknown-publisher behavior; and
  residual risks. Signing, timestamping, and an unavailable official scanner
  can never appear as `PASS`.

- [ ] **Step 5: Apply the private-beta completion rule**

  Label the result **DeckPipe 0.6.0 unsigned private beta** only when every
  non-waived gate is `PASS`, the meaningful 0.5.0 migration is `PASS` or
  truthfully `NOT_APPLICABLE`, all test mutations are reconciled/restored, and
  the final artifact hash still equals `candidate-pin.json`. Never call it
  signed, SmartScreen-trusted, public, production-wide, or generally
  release-ready. If login/MFA, elevation, logout/login, reboot, or an external
  service blocks progress, name the single pending gate and exact resume point
  instead of closing the audit early.

- [ ] **Step 6: Commit the evidence and hand off the artifact**

  Commit only tracked code/tests/docs and redacted audit records; keep builds,
  accounts, backups, media, caches, and raw logs outside Git. Suggested commit:
  `docs(audit): close unsigned private beta rc`. Report the exact D-drive
  staging path, hashes, expected Windows warning, test-account state, Rekordbox
  restore result, and any owner action still required.
