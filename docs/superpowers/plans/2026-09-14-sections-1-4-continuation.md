# DeckPipe sections 1–4 continuation

> **For agentic workers:** Use superpowers:subagent-driven-development for the owned helper repair and independent review. Preserve completed work; use checkboxes for the remaining work.

**Goal:** Continue the first four sections of the Windows release plan, fix the remaining reproducible Stage B setup defects, and produce current verification and a reviewable Windows candidate.

**Architecture:** Keep the implemented catalog, provider clients and AuthBroker. Move the Firefox native-host manifest out of the application installation directory into the current user's LocalAppData, with the PowerShell setup and native executable enforcing the same path and ownership contract.

**Tech Stack:** Windows x64, Python 3.12, PowerShell 5.1, Rust/Tauri 2, Firefox native messaging.

**Spec:** ../specs/2026-09-09-windows-stages-ab-design.md and ../../release-plan-2026-09-09.md sections 1–4.

## Global constraints

- Worktree: `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`, branch `codex/release-hardening`, starting revision `c1830776b7db9bb6aabf4439cc159216192032c9`.
- User instruction of 14 September: implement sections 1–4; Stage C is explicitly deferred. Do not change Rekordbox database code, gates or the live database. Do not introduce Stage D release scope or macOS builds.
- Sections 1–3 and the main Stage B implementation already exist. Do not redispatch the completed September 9 implementation tasks.
- No actual helper registration, browser installation, account access, publication or replacement of existing pinned candidates during engineering work.
- Source tests use an isolated `DECKPIPE_DATA_DIR`; executable tests additionally use isolated child `APPDATA`, `LOCALAPPDATA` and `USERPROFILE`. Never copy working credentials into the lab.
- Keep the existing SoundCloud downloader and provider capabilities. Live Firefox installation/login and real provider acceptance remain distinct from engineering checks.
- Evidence root: `D:\DeckPipe-RC-Lab\qa-evidence\stages-1-4-20260914`. No registry mutations in tests; mock only the registry boundary, exercise real script and filesystem behavior.

## Task 1: Reconcile the implemented first four sections

**Ownership:** controller; plans, evidence and user report.

- [x] Recover the actual implementation branch and September 9 completion ledger; verify clean starting state.
- [x] Re-run the Python suite and frontend/extension checks in an isolated profile.
- [x] Complete the existing PowerShell suites and record current results.
- [x] Update the release plan to separate implemented work, open live acceptance and explicitly deferred Stage C.

## Task 2: Repair per-user Firefox helper setup

**Ownership:** one worker owns `release/auth-helper/Register-DeckPipeAuthHost.ps1`, `Unregister-DeckPipeAuthHost.ps1`, `README.md`, `package_helper.py` if a shared setup file is necessary; `desktop/src-tauri/src/bin/deckpipe-auth-host.rs` and a focused manifest-validation module if necessary; corresponding Rust/native and PowerShell/Python helper tests. No frontend, provider or catalog edits.

**Contract:** The sole new manifest is `%LOCALAPPDATA%\DeckPipe\AuthHelper\native-host.firefox.json`. Registration points the existing HKCU Mozilla host key at that manifest. The native host accepts only this absolute manifest location, the exact Firefox extension ID, expected name/type/extension list and a manifest executable path resolving to itself. It continues to verify the sibling DeckPipe executable and named-pipe peer. No arbitrary-manifest fallback.

**Ownership and recovery:** Registration must not overwrite another installation's manifest or registry entry. Repeated registration for the same installation is safe. An existing legacy registration beside the same installation may be migrated after validating its ownership; launching the updated helper requires re-registration, which the instructions must state. Unregister accepts the original absolute install path even after that directory has been removed, validates the stored manifest ownership before deletion, and removes only the matching registration/manifest. `-WhatIf` makes no changes. Failed registry publication must not destroy the prior manifest or leave an apparently completed registration.

**Required behavior tests:**

```text
1. register with an unwritable install directory and writable isolated LocalAppData -> manifest created only in LocalAppData; registry points there.
2. same install repeated -> same valid registration; another install -> explicit error and original bytes/registry retained.
3. delete synthetic install directory, then unregister with its former path -> owned registration/manifest removed; unrelated entries/files retained.
4. register/unregister -WhatIf -> no file or registry changes.
5. inject registry write failure -> existing manifest/registration preserved; no false success.
6. actual host + new manifest -> native stdio/named-pipe roundtrip succeeds.
7. arbitrary manifest path, mismatched host path, malformed/oversized JSON or wrong Firefox ID -> rejected without credential echo.
```

- [x] Record failing behavior against the current implementation before the fix.
- [x] Implement the aligned script/native path contract and update setup instructions.
- [x] Run covering PowerShell, Python and Rust tests; build the actual helper.
- [x] Obtain independent review, fix material findings and rerun affected checks.

## Task 3: Verify, package and report

**Ownership:** controller after Task 2; integration docs and artifacts. Review the final source diff and keep Stage C untouched.

- [x] Run all affected tests plus the required component build checks; preserve explicit skip and live-acceptance limitations.

The following execution happens **after the source commit is frozen**. Its status is recorded in the per-plan execution ledger and [dated result report](<D:/Claude Code/Projects/deezer-rekordbox-sync/audit/deckpipe-sections-1-4-result-2026-09-14.md>), so recording build results does not change the source revision being verified:

1. Commit reviewed source in the isolated branch and build a new engineering candidate from that exact revision, without replacing the old candidate.
2. Verify installer payload and exercise the final native helper with a synthetic application/profile.
3. Publish the local result report and update the visible release plan. List remaining user-owned browser/account actions; do not mark Stage B accepted without that evidence.

## Scope interpretation

The user explicitly confirmed on 14 September that sections 1–4 of the visible release plan are intended: product scope, global Likes catalog, Stage A and Stage B. Stage C remains deferred. The common-root catalog, missing-only synchronization, existing SoundCloud solution and Windows-only scope remain binding.
