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
