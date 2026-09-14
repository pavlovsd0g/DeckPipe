# Persistent embedded authentication implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Execute the approved plan continuously; do not ask the user to approve the same embedded-browser choice again.

**Goal:** Replace browser extensions with one persistent DeckPipe login window for both streaming services.

**Architecture:** Tauri owns a separate persistent WebView2 profile per provider and submits only the scoped credential directly to the private backend AuthService. Existing account transactions and DPAPI persistence remain; the frontend sees safe status/account summaries. Remove the browser extension/native-messaging transport and its release payload.

**Tech Stack:** Windows x64, Tauri 2.11.5/Wry 0.55.1, Rust, Python 3.12, PowerShell, existing static frontend.

**Spec:** ../specs/2026-09-14-persistent-embedded-auth-design.md

## Global Constraints

- Work only in `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`, branch `codex/release-hardening`, base `e39db3d4c221cc87afbfff1bdc705f5f9437e45e`.
- Windows only. Stage C/Rekordbox, actual music/provider accounts, global browser profiles, existing registrations and pinned candidates are untouched.
- Evidence root: `D:\DeckPipe-RC-Lab\qa-evidence\embedded-auth-20260914`. Temporary files, builds and synthetic browser profiles stay in the lab. Frozen EXE children require isolated `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`; `DECKPIPE_DATA_DIR` alone does not isolate them.
- Source Python: `D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe`; offline Cargo homes are `D:\DeckPipe-RC-Lab\tool-cache\cargo-home` and `D:\DeckPipe-RC-Lab\tool-cache\rustup-home`. Do not fetch or upgrade dependencies.
- Profiles are `%LOCALAPPDATA%\DeckPipe\AuthBrowser\deezer` and `...\sc`, independent of EXE/version. No forced cookie lifetime, no session export from another browser, no secrets in frontend/logs/URLs.
- User approved the unified embedded method; existing default-browser-only conditions are superseded. Preserve both current downloaders and the shared library.
- Workers are not alone in the codebase. Respect others' edits, ownership and untracked evidence; no subagents from workers, no commit/push/install without the controller's handoff.

### Task 1: Persistent native browser and cancellation-safe authentication

**Ownership / files:** `desktop/src-tauri/src/auth_broker.rs`, `auth_runtime.rs`, new `auth_browser.rs`, `main.rs`, `lib.rs`, `permissions/auth-broker.toml`, `Cargo.toml`, `Cargo.lock` only if removing now-unused feature/dependencies requires it, `desktop/src-tauri/tests/auth_protocol.rs`, new native tests/example under `desktop/src-tauri/examples/auth_webview_probe.rs`; remove `src/bin/deckpipe-auth-host.rs`, `browser_bridge.rs`, `native_ipc.rs`. `app/auth_broker.py` and `qa/tests/test_auth_broker.py` only if needed for the validation-round cancellation contract. Do not edit frontend/release scripts or tauri.conf.json resources in this task.

**Consumes:** backend `/api/internal/auth/{complete,commit,discard,logout,status}`. `complete` receives `{requestId,provider,credential}` and returns validationId/account; `commit` receives `{requestId,validationId}`; both launch bearer and private broker header remain mandatory.

**Produces:** `auth_begin(provider)`, `auth_status(requestId?)`, `auth_cancel(requestId)`, `auth_logout(provider)` with PublicStatus per spec; top-level `authMode:"embedded"`, safe accounts and no helper. Logout means forgetting selected provider's DPAPI session and browser data. Old `auth_open_setup` removed. Native module contains the one profile/window factory used by production and synthetic WebView2 proof.

- [ ] Write and run a failing native behavior test before changing production: profile selection/cookie ownership and stale completion after cancel are the first target cases.

```text
profile(LocalAppData, deezer) == LocalAppData/DeckPipe/AuthBrowser/deezer
profile(LocalAppData, sc) != profile(LocalAppData, deezer)
begin(deezer); validate(synthetic_old); cancel(request); finish_validation()
=> no backend commit and no public credential
```

- [ ] Replace native-messaging claims with an in-process, request-bound browser attempt. Native reading is scoped to the provider's HTTPS origin/cookie and exact domains. Use real async Tauri window APIs; no synchronous event-handler deadlocks.
- [ ] Preserve the private prepare/commit transaction and serialize the final commit against replacement/cancel/logout. Allow a new credential after a rejected stale cookie; tombstone timed-out or cancelled backend validation rounds. Never retry identical rejected material every poll tick.
- [ ] Create fixed persistent provider directories with `.data_directory(profile).incognito(false)`, no extension privileges; block unsafe navigation and handle HTTPS login popups in the same isolated profile with identical restrictions. Keep remote windows outside main capabilities.
- [ ] Implement explicit selected-provider logout/clear and failure handling. The main UI receives an error if browser data cannot be cleared; cancellation cannot restore a logged-out account later. A normal close/cancel does not clear the profile.
- [ ] Add covering tests for expiry, replacement, wrong provider/domain/window, invalid-then-new credential, clear failure, selective cleanup and no-secret public results. Replace former native-host protocol tests with the new boundary tests.
- [ ] Add an unshipped example/probe using the same production browser/profile code with synthetic local content; document CLI in the report. It must prove real WebView2 persistence across process restart and selected-profile clearing. Production login still accepts only fixed provider URLs; no runtime test override.
- [ ] Run offline locked Cargo tests/fmt, relevant backend tests, self-review, and report test commands/results and remaining concerns to the controller. No live login or main-profile reads.

### Task 2: Interface and extension-free Windows packaging

**Ownership / files:** `frontend/app.js`, `frontend/index.html`, generated `app/static` and `desktop/ui` through `frontend/build.mjs`, `desktop/src-tauri/tauri.conf.json`, `release/build.ps1`, release packaging docs, `BUILD.md`, `README.md` where current login instructions occur, removal of tracked `extension/*` and `release/auth-helper/*`, `qa/tests/test_auth_helper_package.py`/`test_extension_contract.py`/`auth_helper.test.cjs`/`DeckPipe.AuthSetup.Tests.ps1` replaced or removed as obsolete, auth-related cases in `test_desktop_contract.py`, `test_frontend_build_contract.py`, `test_frontend_contract.py`, `test_stage_ab_frontend.py` and release/orchestrator contract tests in the WORKTREE only. Do not edit native runtime owned by Task 1 without controller coordination.

**Consumes:** exact four Tauri commands and statuses from Task 1. `auth_logout` clears the selected remembered browser session as well as backend authorization. Connected accounts still come from `auth_status(null)`.

**Produces:** one flow for both providers; no Firefox/helper instructions, no `auth_open_setup`, no host executable or extension resources in installers.

- [ ] Write failing frontend behavior and package assertions before the implementation.

```text
click Connect(SC) => invoke auth_begin({provider:'sc'}) once
waiting_browser => text describes DeckPipe login window, never extension setup
cancel => invoke auth_cancel once and stop polling
late status from cancelled/replaced request => cannot render Connected
change account => auth_logout selected provider, then auth_begin that provider
release payload => deckpipe.exe + backend.exe, no auth-host/extension resources
```

- [ ] Replace login copy/buttons, expose remembered-session explanation and explicit "Выйти и забыть вход" / account-switch behavior without technical token fields. Preserve cancellation and visible recoverable errors; prevent stale async responses from overriding new attempts.
- [ ] Remove the obsolete native host build step/features and extension resources. Remove only the specified tracked obsolete files, using explicit paths; never run live unregister or recursive deletion against a computed profile path.
- [ ] Update meaningful current contract tests to the new boundary; retain security assertions and cancellation/no-secret coverage, remove obsolete tests rather than falsely satisfying old implementation-string checks.
- [ ] Rebuild frontend outputs, run affected JS/Python/PowerShell suites, and provide controller report. Commit boundary is controlled by the controller because the full frontend archive test compares generated assets to HEAD.

### Task 3: Integrated proof, candidate and current report

**Ownership:** controller; plans/spec, main audit artifacts, isolated evidence/probe runner, reviewed commits, clean candidate build. Independent final review is mandatory.

- [ ] Review Task 1 and Task 2 separately and resolve material findings before integration freeze; use task briefs/reports/diff packages in this plan's ledger directory.
- [ ] Run the real synthetic WebView2 probe across complete process restart in a Cyrillic lab profile, check persistent cookie/localStorage, profile separation, ordinary close preservation and explicit clear. Record session-cookie limits separately; do not relabel synthetic data as provider acceptance.
- [ ] Commit the reviewed complete source/updated generated assets, run full Python/PowerShell suites, frontend build and offline Rust tests. Confirm main user file unchanged and Stage C source unchanged.
- [ ] Obtain final review of the full authentication change from the recorded base; fix material findings through the implementing worker and rerun covering checks.
- [ ] Freeze source and build a new unsigned engineering candidate into a unique lab staging directory. Read back actual MSI, verify no extension/native-host payload, run extracted backend smoke in isolated child profiles and preserve signing-gate status.
- [ ] Update visible plan/report with current source, actual checks, artifact links and explicit remaining live-login/installer acceptance. Prepare user-owned provider login steps only after all engineering work is concrete and reviewable.

No redundant confirmation is needed between tasks. No merge, push, live registration, account signup or public release is included.
