# DeckPipe release hardening handoff — 2026-08-27

## Objective

Bring DeckPipe from audited 0.5.0 to a releasable Windows build without changing or rotating the user's Kimi, Telegram, Deezer, or SoundCloud credentials. No credential or browser-auth store may be copied into source, tests, reports, backups, or release artifacts.

Development agents must use `gpt-5.5`. Keep delegation to one level: the root agent may dispatch bounded workers, but workers must not spawn subagents.

## Preserved 0.5.0

- Source commit: `be13580427c627135be4a2bc9c287f2cfa7ccd23`
- Annotated tag: `backup/deckpipe-v0.5.0-pre-hardening-20260827`
- Backup directory: `D:\Claude Code\Projects\deezer-rekordbox-sync-backups\2026-08-27-pre-hardening-0.5.0`
- Installed binary source: `E:\DeckPipe` (three program binaries only; no user config included)

| Artifact | SHA-256 |
|---|---|
| `installed-deckpipe-0.5.0.zip` | `F2E5693F079CBD1EFC563B3A46D5A0A16BC2BC9E24EE962FF541B8625D471816` |
| `qa-evidence-2026-08-27.zip` | `C9E8C9D84EDF2F94DCA17EFC22E098AB0823C5E269F9649F15DAE1A84DBFA9BF` |
| `source-history.bundle` | `5AD7288B1B84C9730EB113A5580FF03DE3928ED082C794D17F75A75BB1A45190` |
| `source-tree-be13580.zip` | `7CB2C30796D11C821F015F165464A33CF358CEFD1D09B116CFF30C9627FC6022` |

`git bundle verify` passed and reported complete history. The exact source-tree archive contains only files tracked at the preserved commit. The installed-binary archive contains exactly three entries and no config-like entry.

## Installed development capabilities

- `codex-security@openai-curated` — installed and enabled; supplies targeted security scan, validation, diff scan, threat-model, and local MCP workflows.
- `computer-use@openai-bundled` — installed and enabled; use for the installed Windows EXE, not the in-app browser.
- `context7` MCP — already enabled for current framework documentation.
- `node_repl` MCP — already enabled and is the runtime used by Computer Use.
- `codex-security` MCP — enabled by the installed plugin.

Do not add GitHub/account/cloud MCPs unless a later task specifically requires remote repository or CI actions. They add authentication and trust surface without improving the current local implementation.

## Architecture decisions from primary-source research

### Local security boundary

- Bind the backend to loopback only.
- Authenticate all non-public endpoints with a cryptographically random per-launch secret kept in memory.
- Treat CORS as response-read policy, not authentication. Enforce exact origins plus `Host`, `Origin`/`Referer`, and Fetch Metadata checks.
- Pair the browser extension with a one-time, user-initiated challenge; prefer native messaging with a fixed extension allowlist over unauthenticated port probing.
- Set a strict production CSP, remove inline handlers/eval/remote scripts, set `withGlobalTauri=false`, and minimize window-scoped Tauri capabilities.
- Keep persistent service credentials in Windows-backed secure storage, not plaintext config, localStorage, or Tauri Store.
- Retain and terminate the sidecar handle on app exit; add parent-PID/watchdog behavior and invalidate the launch secret.

### Rekordbox and local state

- Treat `master.db` as integrity-critical reverse-engineered external state. Official Pioneer/AlphaTheta support for third-party writes and locking was not found.
- Use pyrekordbox high-level mutation methods where available and maintain an explicit supported-version matrix.
- Refuse unsafe concurrent writes. Create a consistent backup before mutation, use one bounded transaction, then re-read and reconcile the result.
- Use stable source/target identities and an operation ledger so reruns are idempotent.
- Do not mark sidecar state complete until database commit and reconciliation both succeed.
- Write sidecars to a same-directory temporary file, flush, then replace atomically.
- Add Cyrillic, emoji, combining-mark, NFC/NFD, long-path, and Windows-username round-trip fixtures. Never repair the user's live library without preview and explicit confirmation.

### Release pipeline

- Build PyInstaller and Tauri Windows artifacts on Windows from locked dependencies.
- Keep one canonical product version and build ID across backend, UI, bug report, installer, and QA artifacts.
- Sign and timestamp shipped executables and installer; verify signatures after signing.
- Generate checksums and an SPDX SBOM from the final release directory.
- Test clean install, first launch, upgrade from preserved 0.5.0, uninstall, reinstall, non-ASCII user profile, crash recovery, and orphan-process cleanup.

## Bounded execution and verification policy

Avoid recursive review loops:

1. For each workstream, create one failing regression test or reproducer, implement the fix, and run the targeted GREEN check once.
2. After independent changes are integrated, run the combined unit/integration/build suite once.
3. Run one independent whole-branch review and one bounded fix round for actionable P0/P1 findings.
4. Run Codex Security standard/diff scans during implementation; reserve one deep scan for the release candidate.
5. Run final installed-EXE QA, performance comparison, clean-machine lifecycle, signing, and artifact checks once on the release candidate.

If the same technical approach fails three times, stop that approach, preserve evidence, and escalate the blocker instead of retrying recursively.

## Next-session starting sequence

1. Re-read this handoff and the two HTML audit reports under `audit/`.
2. Confirm the backup tag and bundle, then checkpoint the existing QA/audit infrastructure in Git.
3. Create a release-hardening branch/worktree from `main`.
4. Dispatch GPT-5.5 workers with disjoint ownership: localhost/Tauri security; Rekordbox transaction/reconciliation; lifecycle/atomic state; UI/performance/release pipeline.
5. Integrate in dependency order: security contract, lifecycle/state, Rekordbox correctness, performance/UI, packaging.
6. Apply the bounded verification policy above and update the HTML release report with evidence.
