# DeckPipe Windows Stages A/B Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent owned components; the controller integrates shared API/UI changes and runs whole-change verification. Steps use checkboxes.

**Goal:** Complete approved Windows library integrity and provider/auth work, adding a mandatory common-root music catalog.

**Architecture:** Preserve folder sidecars and downloader implementations, add a persistent catalog with provider identities and actual file locations. Keep provider errors/auth lifecycle explicit and remote writes recoverable. Rekordbox mutation gates remain closed.

**Tech Stack:** Python 3.12/FastAPI/SQLite, vanilla JS/esbuild, Rust/Tauri 2, Windows DPAPI, yt-dlp.

**Spec:** ../specs/2026-09-09-windows-stages-ab-design.md

## Global Constraints

- Work only in D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening; base feafe1fd0bc8851e02be581f221086ce5b9a30b7.
- Preserve dirty main checkout, user music, accounts, master.db and pinned existing installers. No publishing/installing browser extension or global registration during implementation.
- Windows only. Preserve yt-dlp SoundCloud download/likes; playlist creation/parity is not required.
- Test Python: D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe. Always use isolated DECKPIPE_DATA_DIR and PYTHONDONTWRITEBYTECODE=1, no real credentials.
- Tests demonstrate failures before fixes; do not weaken security or test contracts to mask failures. Generated frontend assets are rebuilt by controller.
- Each worker owns only listed files; no reverting other workers, no nested agents. Controller stages explicit paths and owns integration commits.

### Task 1: Local identity, publication and recovery (Stage A)

**Ownership:** app/atomic_io.py, app/library.py, app/jobs.py; new app/rename_journal.py if needed; qa/tests/test_crash_safety.py, test_isolated_core.py and a new test_stage_a_integrity.py. Do not edit main.py, frontend, auth or catalog module.

**Interfaces:** Preserve public library/jobs signatures. `scan_playlist(pl_dir, tracks)` remains local-folder scan; controller will layer global catalog afterward. Retain provider-qualified `track_key`. Global references are not introduced by this task.

**Requirements and test cases:**

```python
# Temporary directories and synthetic file bytes only.
# Wanted Artist/Home must not adopt Other Artist - Home.wav.
# Song (Extended Mix) before already mapped Song must not take Song's file.
# Two equally plausible filenames must not silently choose enumeration order.
# Two IDs with same title/artist and numbering=False publish distinct paths/bytes.
# Existing unowned media may never be overwritten by a new download or WAV step.
# primary absent + valid .bak + injected failed replace => .bak stays loadable.
# rename + injected failed sidecar save => paths/map consistent after recovery.
# child process exits after physical rename => fresh process load recovers mapping.
```

- [x] Write tests using actual filesystem operations and minimal synthetic downloader/converter adapters; run and record expected old failures.
- [x] Make exact artist/full-title matching preserve mix/version; pre-reserve valid sidecar-owned files before any match. Ambiguous matches are not ready.
- [x] Allocate provider/ID-safe output names on collisions; protect publication against existing unrelated paths, including WAV output and retried conversion.
- [x] Treat valid backup-only state as previous JSON state; preserve last good copy on all publication failures.
- [x] Journal planned rename before file moves; recover deterministically on load after failure/restart. Never clobber destination or remove evidence on incomplete recovery.
- [x] Ensure source provider and source_file/stage remain on failed conversion; this task must not introduce full-redownload retries.
- [x] Run affected suites plus new regression suite; write task report with red/green commands/results and limitations.

### Task 2: Persistent common music catalog and root requirement (Stage A)

**Ownership:** controller: app/library_catalog.py, new catalog tests; integration with library/jobs after Task 1; library routes in main.py after Task 3; frontend app/index/styles in Task 5.

**Interfaces:** `MusicCatalog(db_path).scan(roots) -> summary`, `match(track) -> {status, locations, match_source}`, `confirm(track,path)`; module helpers derive db path from deezer_client.ROOT. Scanned file rows persist paths/size/mtime/duration/artist/title/format/availability; identities use `(provider,track_id,path)`.

```python
# Artist - One.wav at root/Folder A, Artist - Two.wav at root/Folder B.
# scan + match likes [One, Two, Three] => ok, ok, missing; two actual folders.
# restart catalog object => same results; repeated sync creates no duplicate.
# conflicting same title/artist files => ambiguous; confirm picks explicit path.
# removed file => missing; unavailable root => offline, not empty/missing.
```

- [x] Write regression tests for cross-folder likes, tagged manual audio, sidecar identities, conflicts, restart, move/delete/offline, overlapping roots and no symlink escape.
- [x] Implement SQLite catalog with safe recursive scan and actual file recheck; use sidecars as identity evidence, retain unmatched files.
- [x] Require explicit existing global root before scans/downloads. Accept a previously saved valid root; retain explicit playlist destination bindings.
- [x] Integrate catalog match in playlist read and download preflight; show actual locations without creating duplicate sidecar ownership or renumbering external files.
- [x] Add explicit rescan/status/confirm routes and test idempotent repeated actions.

### Task 3: Complete provider collections, explicit errors and durable remote results (Stage B)

**Ownership:** app/main.py provider functions/routes only (fetch_playlists/fetch_tracks/search/album/api_playlists/provider errors/retry/remote add); app/soundcloud.py; new app/provider_errors.py and app/remote_actions.py if useful; qa/tests/test_provider_collections.py, test_provider_retry.py, test_remote_actions.py. Do not modify library/config/local-catalog routes, login/auth routes, frontend, jobs, security or Rust. Controller will integrate those later.

**Interfaces:** Existing success shapes remain compatible (list playlists, tracks). Non-success uses HTTP detail object `{code, service, message, retryable}` or additive per-service `errors` for partial search. Remote search/download response retains `job_id`, `added_to_deezer` and adds `remote_action`; persisted remote operations expose list/retry endpoints. Original provider in error track is authoritative, never destination playlist prefix.

```python
# get_user_playlists first=50 after=None then after=cursor; 120 items => 3 calls.
# has_next_page with repeated/empty cursor fails explicitly, never caches partial.
# long album tracks.next is followed only on trusted api.deezer.com endpoint.
# network exception from playlist fetch => HTTP 503, not [] 200.
# SC track in local:test retry => provider=sc, raw ID (not sc:sc:id).
# local download enqueued + Deezer add fails => durable remote_action failed.
# retry after uncertain response checks remote membership before adding missing IDs.
```

- [x] Write/run failing tests for pagination, partial errors, original-provider retry and restart-safe remote result.
- [x] Inspect installed deezer-python-gql pagination model signatures, implement complete collections with loop guards, safe origins, request timeouts and no secret error bodies.
- [x] Retain SoundCloud yt-dlp download/likes; report partial/inaccessible collection explicitly. Handle pages and incomplete metadata without silently claiming completeness. Do not require SC playlist creation or add OAuth.
- [x] Fix error record provider/raw ID and retry stage propagation, including mixed local playlists and renamed destination.
- [x] Persist failed Deezer add independently from local job; retry reconciles membership first, protects against duplicate mutation after uncertain success, exposes status/retry.
- [x] Run affected suites, write report with exact results and integration contract. Do not stage/commit files owned by others.

### Task 4: Default-browser AuthBroker and Windows browser helper (Stage B)

**Ownership:** app/auth_broker.py, app/browser_bridge.py, app/secure_store.py logout additions, run_backend.py native-host mode; extension files; desktop/src-tauri/src/main.rs and owned config/capabilities; release helper packaging only; auth/bridge/extension tests. main.py/frontend integration belongs to controller.

**Design input:** D:\DeckPipe-RC-Lab\qa-evidence\audit-2026-09-09\stage-b-auth-design.md, after bounded design completes. Actual default browser is Firefox; no Chrome-only assumption.

**Interfaces:** Auth begin/provider/status/cancel/complete/logout are shared lifecycle methods; begin/status return only public state/ID/expiry. Extension sends credentials directly through native host into backend validation and DPAPI. Never return provider secrets to JS UI, store them in URLs/logs or export a browser profile. Existing yt-dlp/session clients remain.

```python
# begin -> pending; complete with expected one-time state/provider -> connected.
# expired/cancelled/replayed/wrong-provider completion => rejected, no store writes.
# native message oversized/malformed/wrong extension source => rejected.
# logout clears provider only, sessions/caches invalidated, local files preserved.
```

- [x] Finalize concrete helper design using official browser docs; record external signed-extension/install dependency explicitly.
- [x] Write/run lifecycle and native message security tests before code.
- [x] Implement broker, restricted bridge, provider validation, DPAPI lifecycle, fixed-URL system-browser launcher and account-scoped invalidation hooks.
- [x] Build helper extension package and explicit per-user native host registration/unregistration assets; do not install in current browser or mutate registry during implementation.
- [x] Run Python/JS and Rust verification; report exactly what needs actual user browser/setup rather than claiming synthetic auth is live proof.

### Task 5: UI integration, updated final plan and Windows-only docs

**Ownership:** controller frontend/app.js,index.html,styles.css; main.py integration after Task 3/4; generated assets; audit report and user docs.

- [x] Verify with DOM contract tests mandatory root selection, binding with differing title, actual file location, ambiguity resolution and missing-only action.
- [x] Implement clear initial root selection/rescan status and per-track folders; preserve safe DOM and keyboard paths.
- [x] Display provider/remote-operation partial results, retry controls and browser login lifecycle; remove manual token input primary UX and embedded login code.
- [x] Mark Windows-only scope and SoundCloud required capability subset in final plan/report; explain existing yt-dlp technical capability without claiming it grants rights itself.
- [x] Rebuild frontend/static and desktop/ui, check generated equality and syntax.

### Task 6: Verify and close only achieved work

- [x] Run all Python/PowerShell suites; targeted real-file/cross-folder/lifecycle/hostile-request regressions, frontend build, Rust check/test.
- [x] Independent review of data integrity and auth boundaries; fix material findings and rerun affected checks.
- [x] Build reviewable Windows candidate/helper without replacing pinned old artifacts or installing into user's profile.
- [x] Update completion ledger with source revision, new evidence, remaining external Firefox signing/install or live-account gates. Never label A/B complete if a required functional or external gate remains.

Engineering verification above was completed on 9 September at `c1830776b7db9bb6aabf4439cc159216192032c9`; the original ledger and `audit/deckpipe-stages-ab-result-2026-09-09.md` record the evidence and profile-isolation incident. These boxes record that completed engineering work, not live acceptance of Stage B. Continuation authorised on 14 September is tracked in [sections 1–4 continuation](2026-09-14-sections-1-4-continuation.md). Stage C is explicitly deferred.
