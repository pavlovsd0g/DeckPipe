# Rekordbox Stage C implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Execute the approved Stage C continuously, with task reviews and one final review; do not ask the user to approve C again. Steps use checkboxes.

**Goal:** Synchronize ordered streaming/local playlists from the common library into Rekordbox, preserving its data and supporting verified reversible WAV paths.

**Architecture:** Keep pure planning, isolate the pinned database adapter and durable recovery as focused modules, feed them authoritative catalog paths, then expose preview/hash-bound apply in the UI. Native authentication and downloaders remain unchanged.

**Tech Stack:** Windows x64; Python 3.12, FastAPI, SQLAlchemy/SQLCipher, pyrekordbox 0.4.4, existing static JS/Tauri 2.

**Spec:** ../specs/2026-09-15-rekordbox-stage-c-design.md

## Global Constraints

- Worktree `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`, branch `codex/release-hardening`, base `2314607c13a94767ba28b62ca1c6754d8fbe7cbb`. Main is dirty and must remain intact.
- Evidence under `D:\DeckPipe-RC-Lab\qa-evidence\rekordbox-stage-c-20260915`; backups/fixtures/builds there or other new D-lab owned paths. Never open/discover/copy/write the actual user's master.db or keys for tests. Explicit synthetic pyrekordbox fixture connection parameters only.
- Existing Python `D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe`. Offline pinned dependencies; no upgrades. All frozen EXE child runs isolate APPDATA, LOCALAPPDATA, USERPROFILE, not just DECKPIPE_DATA_DIR. Use a short lab TEMP for Windows PowerShell tests.
- A/B/authentication are retained. Stage C deferral is superseded by the user's 15 September request. Stage D/live provider/installer lifecycle is outside this task.
- Workers are not alone: preserve others' changes, respect ownership, no subagents, no commit/staging ignored reports, no merge/push/install. Root controls commits and review packages.
- Preserve ContentIDs, retained membership IDs, comments, cues/grid and other playlists; no-op writes zero. Never replace missing/unavailable remote data with an empty destructive desired list.
- Apply requires exact confirmation plus viewed plan hash under a crash-released lock. No unrelated experimental feature flag is required in normal product use; this replaces the old environment-only gate. No actual user-data mutation is authorized by this development plan.

### Task 1: Identity-preserving database transaction and crash recovery

**Ownership:** `app/rekordbox.py`; new `app/rekordbox_adapter.py` and `app/rekordbox_recovery.py` if needed to keep responsibilities focused; `qa/tests/test_rekordbox_sync.py`, new `qa/tests/test_rekordbox_database.py`, `qa/tests/rekordbox_fixture.py` and recovery-specific tests. Do not edit main.py/frontend/catalog/converter.

**Consumes:** desired track dictionaries from the spec, actual installed pyrekordbox API/schema, atomic_io mechanisms where appropriate.

**Produces:** `sync_playlist(pl_name, ordered_files, create_missing=True, *, dry_run=True, adapter_factory=None, confirmation_token=None, expected_plan_hash=None, playlist_id=None, operation_kind="sync", on_reconciled=None) -> dict`; pure `plan_playlist_sync`; `get_rb_playlists` includes unique IDs. Add explicit adapter parameters for synthetic tests, never an HTTP arbitrary-database input. Report any internal extension before consumers start.

- [x] Write RED behavior tests that expose destructive comment/member rewriting, repeated apply writes, stale preview, WAL/backup loss and recovery after process death. Use real DB fixture for mutation semantics and keep focused doubles for failure injection.

```python
preview = sync_playlist("Likes", desired, adapter_factory=fixture.factory)
assert fixture.inventory() == original_inventory
result = sync_playlist("Likes", desired, dry_run=False,
    confirmation_token=APPLY_CONFIRMATION_TOKEN,
    expected_plan_hash=preview["plan"]["hash"], adapter_factory=fixture.factory)
assert result["reconciled"]
assert fixture.protected_content_and_cue_fields() == original_protected_fields
before = fixture.inventory()
again = sync_playlist("Likes", desired, dry_run=False,
    confirmation_token=APPLY_CONFIRMATION_TOKEN,
    expected_plan_hash=result["plan"]["hash"], adapter_factory=fixture.factory)
assert again["unchanged"] and fixture.inventory() == before
```

- [x] Implement exact target/path identity, preserve existing metadata/comments and retained memberships, reject duplicate targets/paths, use explicit single transaction and supported registry primitives. Remove/reorder only the target playlist's memberships; path-only mode cannot change membership.
- [x] Implement fingerprint-bound preview/apply and no-op/replay. Check root files and DB state under the lock immediately before mutation. Confirmation/hash missing returns before write-adapter construction; changed state returns `stale_preview` without backup/mutation.
- [x] Implement verified consistent backup, durable operation phases, atomic external-file publication, integrity/readback and recover-or-refuse after a killed process. Test rollback failures separately; preserve damaged evidence for diagnosis.
- [x] Test real newly generated SQLCipher DB + XML/ANLZ fixtures, protected cues/grid/ContentID, WAL, competing lock, interruption and reopening. Direct-session methods must preserve pinned registry semantics; update old tests that specifically asserted the superseded destructive behavior, retain their safety scenarios.
- [x] Run targeted Python unittest files in the isolated lab; self-review and report RED/GREEN, actual interfaces, remaining limits. Controller commits/reviews before Task 2.

### Task 2: Authoritative catalog input and verified reversible WAV paths

**Ownership:** `app/main.py` Rekordbox endpoints and source resolution, plus narrowly required local membership persistence/read projection; new `app/rekordbox_service.py`, `app/rekordbox_media.py`; minimal library/catalog/converter helpers only when required; new `qa/tests/test_rekordbox_catalog.py`, `qa/tests/test_rekordbox_media.py`, API cases formerly in test_rekordbox_sync.py with coordination. Do not edit reviewed core/adapter without reporting a concrete interface gap first.

**Consumes:** Task 1 sync contract; server-side Deezer fetch_tracks, SoundCloud source fetching and local sources; catalog_service.playlist_tracks/configured roots; staged converter/publication.

**Produces:** `/api/rb/sync` previews/applies resolved authoritative order with body expected_plan_hash/optional playlist_id. `/api/rb/prepare-wav` returns safe preparation counts/state without writing Rekordbox. `/api/flip` defaults read-only preview, uses same viewed hash/confirmation for path-only application. A read-only playlist media-state response must let the frontend choose WAV vs original reliably after restart. Report exact response fields/routes before Task 3.

- [x] Write RED API tests using real temporary audio in two nested folders plus explicit external binding: Likes resolves all paths, gaps/errors/ambiguous/offline roots block, local SC identities are not double-prefixed, source order and library changes invalidate apply.

```python
preview = client.post("/api/rb/sync?dry_run=true", json=likes_request).json()
assert [t["path"] for t in preview["plan"]["desired_resolved"]] == expected_cross_folder_paths
assert not fake_core.did_mutate
# A new or removed provider track must force another preview.
provider_tracks.append(new_track)
result = apply_with_hash(preview["plan"]["hash"])
assert result["error"]["code"] == "stale_preview"
```

- [x] Replace sidecar-only sourcing with authoritative service order and catalog paths; keep all unreadable/unavailable items explicit. Preserve normal sync as an idempotent operation without arbitrary path input.
- [x] Close the existing local-source producer gap: explicit local additions must retain requested membership/order even when all files are reused and no download job runs. Provide a validated local tracks projection through the catalog without calling Deezer for a local key. Keep unresolved requested members explicit and preserve existing local metadata.
- [x] Write real-audio RED tests for WAV preparation/reuse/revert and timeline mismatch: truncated/shifted variant or changed original cannot rebind; preserved cues/grid keep their timestamps. Use source/target decoded PCM equivalence and native rates/channels/frame counts, not stream-reported duration alone.
- [x] Implement durable original/variant mapping, staged no-clobber publication, explicit preparation action, fresh verification at application, source preservation and post-reconcile state/recovery. Ordinary preview writes no media or database. Expose shared-content effects in preview.
- [x] Run targeted API/audio suites; demonstrate real converter and ANLZ preservation; report exact contracts and no live-provider claims. Controller commits/reviews before Task 3.

### Task 3: Usable preview, confirmation, and truthful results

**Ownership:** `frontend/app.js`, `frontend/index.html`, styles only if necessary; generated app/static and desktop/ui via build; frontend behavior tests for changed flow. No native/backend implementation.

**Consumes:** reviewed Task 1/2 routes/result fields. Backend confirmation literal is transmitted internally after the user confirms the visible preview; do not ask the user to type a technical token.

**Produces:** normal Windows UI for target/order/path preview, applying exactly the displayed hash, unresolved and stale-state handling, no-op, recovery errors, WAV preparation and reversible path switch. All Russian user messages describe actions/results rather than internal JSON/hash/experimental flags.

- [ ] Write failing behavior tests for no apply on cancellation/unresolved/error, exact hash forwarding, changed selected playlist while awaiting preview, duplicate apply prevention and readable result messages.

```javascript
const preview = await rbSync();
// Harness holds the first preview then selects another playlist.
// Releasing the old response must not apply or display it as the new selection.
if (calls.some(call => call.isApply && call.hash !== displayedHash)) throw Error('wrong preview applied');
```

- [ ] Implement preview display with target/counts/actual paths, explicit confirmation and stale-result guards. Retain all source/account/library behaviors. Use optional target selection for duplicate playlist names; unresolved results cannot be applied.
- [ ] Implement WAV prepare -> database preview -> explicit apply, revert, persisted mode and shared-content notice; show success only after reconciled backend result.
- [ ] Expose existing local playlists through the normal selection UI using the reviewed local source projection, so their C controls are reachable. Use the existing layout and canonical local keys; retain provider tabs and search behavior.
- [ ] Run affected FakeDOM/UI contracts, regenerate frontend, report paths/results. Controller commits before full archive checks.

### Task 4: Integrated evidence, new Windows candidate and report

**Ownership:** controller; current release plan/audit, task ledger/review packages, lab harnesses, source gate and exact-revision build/readback. Fresh final reviewer mandatory. Do not reuse completed embedded-auth ledger as this plan's ledger.

- [ ] Review each completed task and fix material findings through its owner. Record task status and source commits immediately.
- [ ] Execute isolated real DB crash/recovery, catalog/WAV and frontend boundary proof; ensure actual fixture DB/file inventories and precise integrity outcomes are recorded.
- [ ] Commit complete source; run full Python/PowerShell suites, frontend build and offline Rust checks. Preserve main user's Orchestrator file hash and A/B/authentication boundary.
- [ ] Run one final whole-change review, one coherent fix wave if needed, and scoped re-review. Tie evidence to final source revision.
- [ ] Build fresh unsigned engineering Windows candidate, validate MSI payload/artifact provenance (Tauri bundle marker exception must be exact), exercise extracted backend on isolated synthetic fixtures. Preserve public-release signing gate.
- [ ] Update visible release plan and Stage C report with code, actual test/build outcomes, remaining real Rekordbox/live-provider/installer acceptance. No merge/push/install/pin promotion. Retain worktree/evidence for further release work.
