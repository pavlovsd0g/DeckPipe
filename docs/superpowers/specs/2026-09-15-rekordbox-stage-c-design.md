# Stage C: Rekordbox sync from the common library

User authorization: «ок приступай к C» on 15 September 2026 resumes section 5 of `docs/release-plan-2026-09-09.md`. The former C deferral is superseded. Implementation continues in the existing isolated worktree; the user's production database is not a development fixture. This design makes the already-approved C requirements concrete; it does not request another approval for C.

## Scope and existing problems

Windows x64, existing Python/FastAPI/Tauri UI and pinned pyrekordbox 0.4.4. Build on A/B and embedded authentication at `2314607c13a94767ba28b62ca1c6754d8fbe7cbb`. Existing `app/rekordbox.py` has a pure diff and rollback coordinator, but its adapter replaces playlist memberships, trusts/overwrites comments as streaming identities, copies only the main database file, lacks durable crash recovery and is covered mainly by doubles. The API only reads a playlist folder sidecar. `/api/flip` is disabled. These are the C work surfaces.

No macOS, provider-auth changes, public distribution, installer lifecycle, automatic synchronization or user-database mutation during development. Stage D and real-provider acceptance remain separate. Keep main-checkout edits, actual music, browser profiles and the pinned prior candidate intact.

## User behavior

The user selects a playlist or Likes. DeckPipe resolves its complete ordered source list against the common music catalog, including explicitly bound folders. Every track must have an unambiguous readable local file; missing/offline/partial/ambiguous/failed tracks block application rather than silently yielding a destructive partial playlist. Source fetch failures cannot mean an empty successful collection. A genuinely empty playlist has an explicit removal preview.

Sync first shows a read-only preview: target playlist, additions, membership removals, moves, path changes, unresolved items and actual local paths. Existing same-name targets must be unambiguous or selected by ID. Removal affects only membership in the selected playlist, never the content collection or another playlist. Apply requires explicit user confirmation and the exact preview hash. Recompute authoritative database/file state under the mutation lock; a changed preview is rejected. A repeated successful operation, or an already identical playlist, does not rewrite database rows, USN, XML, ANLZ or create another backup.

Existing ContentIDs, playlist-song IDs for retained memberships, comments, cues/hot cues, beat grid, BPM, ratings, tags and manually edited metadata stay intact. New content uses the existing high-level importer. Resolve existing content using canonical Windows paths and explicit verified source/target aliases, not an arbitrary comment or first duplicate match. Ambiguous identities fail closed. Do not encode provider IDs into user comments.

## Database and file transaction

Use the pinned pyrekordbox schema/ORM/registry primitives. Some helpers commit internally (including removal); inspect the actual installed version and avoid those helpers inside a logical transaction. Use one explicit transaction for database changes, preserve registry/USN semantics, and reconcile from a fresh handle after committing. Verify database integrity before/after. Check Rekordbox is closed before opening a write path and immediately before mutation; refuse a busy database. Do not auto-close the user's application.

The cross-process lock must be released by the OS after process death; an abandoned lock filename cannot permanently block recovery. Backups must be consistent with SQLite/SQLCipher WAL state. Include all affected ANLZ, masterPlaylists6.xml, identity/media bookkeeping written in this transaction, their original existence and verified hashes. Use bounded atomic publication for external files. Never overwrite an unrelated original after it has changed externally.

Maintain an atomically written durable operation journal with operation identity, expected inputs, backup inventory/hashes and phases. Persist recovery data before the first mutation. Following interruption, a subsequent explicit apply/recovery under the lock either validates completion or restores the recorded pre-operation state with all handles closed, then verifies it through a fresh connection. Detect damaged/missing backup, running Rekordbox, an unrecognized database or external changes and return a specific recovery-needed error; do not guess or discard the journal. Preview/status can report recovery needed but do not silently mutate the target. Exercise real subprocess termination between database and external-file boundaries.

## WAV

WAV preparation is a distinct local-file operation, not a dry-run database mutation. Use the existing staged converter/publication mechanisms, preserve originals and avoid overwriting existing files. Keep durable verified original/variant mapping, including cross-folder tracks and file fingerprints. Reuse a verified variant on repeat.

Before rebinding an existing ContentID, verify source and target represent the same timeline: sample rate/channel count/duration/frame count and normalized decoded PCM equivalence at the produced bit depth. Reject truncation, inserted silence, changed source, wrong variant and unavailable files. Leave cue/beat timing data untouched; update only the path portions of analysis files and required file-format fields. Test actual converter output and real ANLZ path round trips with timing markers retained. Reverting uses the retained verified original. Never delete source audio or change playlist membership as part of a path-only flip. Show that a shared ContentID path changes for every playlist using that content.

WAV preparation may create files after its explicit action, but database preview never does. Failure to apply leaves those valid local variants available without claiming the Rekordbox switch succeeded. Media-mode/bookkeeping advances only after database reconciliation and can be reconstructed/recovered if the process dies after commit.

## Contracts and implementation boundaries

Keep the public result shape (`dry_run`, `applied`, `reconciled`, `plan`, `unresolved`, `backup_id`, `error`) and add `unchanged`, `recovery`, file/target context as needed. Extend `sync_playlist` with `expected_plan_hash: str | None`, `playlist_id: str | None`, and `operation_kind: str = "sync"` (`"relocate"` is path-only). Desired entries retain provider_id/title/artist/album/duration/position/path; optional `source_path` is an explicitly verified alias for relocation. The planner/coordinator must include relevant target, database, file and analysis fingerprints in the preview hash. Exact canonical names and extras are reported by Task 1 before consumers implement them.

Apply requires `APPLY_REKORDBOX_CHANGES` plus the viewed hash. The obsolete environment-only experiment switch is superseded by this enforceable preview/apply contract, so the implemented user flow works in a normal Windows launch. This is product-level explicit confirmation, not permission for the coding agent to modify the user's database. A direct call without confirmation/hash is rejected before constructing a write adapter.

API `/api/rb/sync` fetches authoritative Deezer/SC/local source order on the server and resolves paths through the catalog; no caller-supplied arbitrary file list. `RbSyncIn` adds `expected_plan_hash` and optional target `playlist_id`. `/api/rb/prepare-wav` prepares local variants; `/api/flip` adopts the same read-only preview and hash-bound apply pattern using verified mappings. Exact preparation/state responses are recorded before the frontend task. Keep API bearer/origin controls unchanged.

## Verification and acceptance limits

- Pure diff/adapter tests: content and retained membership identity, Unicode/NFC/NFD, duplicate names/paths, exact no-op, stale preview, protected metadata, other playlists untouched.
- Real pinned pyrekordbox integration on newly generated SQLCipher fixtures using explicit synthetic keys/paths: dry-run zero writes, apply/reopen/integrity, WAL consistency, persistent journal, OS lock after termination, recovery and failed restore. No cached production key or database discovery in tests.
- API/catalog tests with real temporary audio across multiple roots, fetched-order changes, errors, disconnected/partial roots and wrong versions. Source identity gets one provider prefix, including SC tracks in local lists.
- WAV converter and timeline tests, original/variant/revert, ANLZ cue/grid preservation and cross-playlist scope.
- UI behavior tests for preview, disabled apply on unresolved/error/stale state, explicit confirmation, no-op and selected-playlist changes during async operations.
- Full current Python/PowerShell suites, deterministic frontend build, offline Rust tests/build, independent task/final reviews, clean exact-revision engineering installers, MSI readback and extracted-backend checks.

Synthetic fixtures establish engineering behavior, not live compatibility with an arbitrary installed Rekordbox version. Report that boundary precisely. A real collection or live Rekordbox UI is not required to implement and test the transaction safely; no real-account approval is requested before the engineering work is complete.

Primary API reference checked: [pyrekordbox 0.4.4 database API](https://pyrekordbox.readthedocs.io/en/stable/generated/pyrekordbox.db6.database.html). The pinned installed source is authoritative for helper commit behavior.
