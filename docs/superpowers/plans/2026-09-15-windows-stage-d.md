# Stage D: installed Windows and live collection acceptance

User instruction, 15 September 2026: "приступай, можешь брать пк под управление. Рекордбокс предлагаю не клонировать копией а проверять на моей". This authorizes Stage D and testing the actual Rekordbox collection. The earlier synthetic-only Stage C boundary is superseded for this stage. A verified rollback backup is retained; the test target is the actual collection, not a cloned test database.

## Current baseline and constraints

- Worktree `D:/Claude Code/Projects/deezer-rekordbox-sync/.worktrees/release-hardening`, branch `codex/release-hardening`, entry HEAD `d30a5f65e9b9df809a63de9130183fc602b9b904`. Stage C product/test/build evidence is bound to `6b3d8ab26a1b48edb6b22a07db4f8c8f5b3ba1a5`; the difference is documentation only.
- Preserve main's existing Orchestrator edit, SHA256 `15ca74f28c1c808798208d2452f2f7d9373a839f5080ee613e3cf44464cead6c`. No merge/push or publication is authorized.
- Existing installed DeckPipe0.6.0 is the 28 August build under `D:/DeckPipe-RC-Lab/install/DeckPipe-0.6.0-private-beta`. Rekordbox7.2.14.0323 is installed on F:. Neither process was running at preflight. The current user's actual master.db and masterPlaylists6.xml are present under APPDATA/Pioneer/rekordbox.
- Existing standard test profile `C:/Users/DeckPipe_Тест_0828` is present and unloaded. Switching user/login/UAC may require the owner; passwords and provider credentials are not exported, read or logged.
- Keep labs, generated media, candidate outputs, backups and redacted evidence under `D:/DeckPipe-RC-Lab`. The pre-existing private-beta signing waiver remains an explicit channel constraint, never a signed/public PASS. Preserve old candidate pin/evidence; a new candidate gets its own stage and pin.
- Use the current Stage C exact-preview and separate recovery contracts. Old handoff references to an environment-only experiment flag or browser extension are historical and must not be reinstated.
- Windows UI uses the provided Computer Use API. Do not substitute raw PowerShell UI Automation for a failed tool. Provider authentication, MFA and security prompts remain owner actions.

## Execution checklist

1. **IN PROGRESS — Candidate and host preflight.** Verify installed version/hash, actual collection location, available storage, existing Windows test profile, rollback inventory and current source. Prepare a fresh private-beta candidate for installed acceptance using the pinned offline pipeline; preserve engineering Stage C artifacts.
2. **PENDING — Installed lifecycle.** Upgrade the existing install, verify payload hashes/provenance and settings/session preservation; clean-profile first run, restart, uninstall/reinstall, listener/process cleanup and Unicode account. Validate NSIS/MSI behavior separately; do not relabel extracted backend tests as installed GUI evidence.
3. **PENDING — Live providers.** Login/cancel/expired session, owner login/MFA, persistence across process restart, Likes across the common library, bounded sample download per provider, missing-only repeat and clear failures. Use installed app; retain provider differences and no browser extension.
4. **PENDING — Actual Rekordbox collection.** Prove Rekordbox closed, inventory/hash current DB and affected analysis/XML files, create and read back a consistent rollback backup, validate schema/integrity, inspect preview, and exercise a uniquely named QA playlist with existing real tracks. Verify IDs/cues/grid/manual fields/other playlists, no-op, WAV/original return and native Rekordbox readback. Restore the pretest collection and verify preservation; never overwrite an unrelated concurrent edit or conceal a failed restore.
5. **PENDING — Scale and media availability.** Large common library, explicitly attached folders, missing media/disconnected device, bounded performance and recovery. Do not disconnect a live user drive or delete original music merely to inject a failure.
6. **PENDING — Closure.** Fix actual findings with focused RED/GREEN plus affected verification and final candidate rebuild when product changes. Record installed/live outcomes, candidate hashes, open owner interactions and the precise release channel. Documentation and source tests alone cannot close D.

## Initial tool issue

The Windows `node_repl` tool failed before executing initialization with `failed to write kernel assets: ... os error3`. Resetting its kernel and registering the available Node package root did not resolve it. No Windows app input or security setting was changed. Native GUI acceptance is pending a working tool session; independent filesystem/CLI/database preflight can continue.

Evidence root: `D:/DeckPipe-RC-Lab/qa-evidence/windows-stage-d-20260915`. This document records work in progress, not a completed release.
