# DeckPipe 0.6.0 unsigned private-beta RC execution handoff

**Date:** 2026-08-28
**Mission:** Continue implementation, execute every remaining RC gate, and
deliver one verified **unsigned private-beta** candidate. This is an execution
handoff, not a request for another plan or audit-only pass.

## Start here

Work only in:

`D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`

Branch: `codex/release-hardening`

Start from the commit containing this handoff. The immediately preceding design
baseline is `0f8019d0c516aebda633f6e65c98d2fbe808748f`; the last product implementation
commit is `ff79898`; the historical engineering audit is `d261c79`. Require a
clean worktree before implementation and record the actual full `HEAD` rather
than assuming it.

Never edit, clean, reset, switch, or build from the main checkout:

`D:\Claude Code\Projects\deezer-rekordbox-sync`

At handoff time its `main` branch is ahead by one commit and has user-owned
changes in:

- `qa/tests/DeckPipe.Orchestrator.Tests.ps1`
- `qa/tests/DeckPipe.QA.Tests.ps1`

Those files in main are not an alternate source of truth. The worktree has its
own clean versions.

Use `gpt-5.5` with high reasoning for the execution session if the user is
choosing a model. Do not create nested agents by default. If an applicable
execution/review skill explicitly uses agents, use a fresh bounded implementer
and one fresh whole-branch read-only reviewer; tell both that other work may be
present, forbid the reviewer from edits/commits, and do not delegate user login,
UAC, logout, reboot, or live-Rekordbox judgment.

## Required reading order

Read each file completely before changing code:

1. `docs/superpowers/specs/2026-08-28-deckpipe-private-beta-rc-closure-design.md`
2. The dated addendum beginning at “Owner-approved private-beta RC closure
   addendum — 2026-08-28” in
   `docs/superpowers/plans/2026-08-27-deckpipe-release-hardening.md`
3. This handoff
4. `audit/deckpipe-release-hardening-verification-2026-08-27.md`
5. `release/README.md`, `release/build.ps1`, `release/verify.ps1`, and the tests
   named by the addendum

Apply the `execute-plan-doc` or `superpowers:executing-plans` skill, as
available, and the mandatory TDD/verification skills. Use the approved addendum
as the task checklist; update it only with truthful completion evidence.

## Shell, file, and Git discipline

- Prefix supported shell commands issued through Codex with `rtk`; use
  `rtk proxy COMMAND` only when RTK does not support the command. Use
  `rtk gain`/`rtk gain --history` for RTK diagnostics.
- Use native PowerShell on Windows. Use `rg`/`rg --files` for searches.
- Use `apply_patch` for source/document edits. Preserve unrelated/user changes.
- Add a failing test before every product or release-tool behavior change.
- Make the bounded commits named in Tasks 9–15; do not squash away evidence.
- Never use `git reset --hard`, `git checkout --`, or a broad recursive delete.
- Before deleting/replacing a failed lab stage, resolve the absolute path and
  prove it is below the exact intended `D:\DeckPipe-RC-Lab` subdirectory.
- Keep wheelhouses, venvs, downloads, installers, backups, raw logs, media, and
  test profiles out of Git.
- Do not publish, upload, push, create a PR, or distribute an artifact unless the
  user separately asks. The deliverable is a local private-beta candidate.

Initial preflight:

```powershell
rtk git status --short --branch
rtk git log -5 --oneline
rtk git -C 'D:\Claude Code\Projects\deezer-rekordbox-sync' status --short --branch
rtk proxy powershell.exe -NoProfile -ExecutionPolicy Bypass -File qa/tests/DeckPipe.Release.Tests.ps1
rtk proxy ..\..\.venv\Scripts\python.exe -m unittest discover -s qa\tests -p 'test_*.py'
```

The explicit venv executable is intentionally run through `rtk proxy`; do not
silently fall back to the Microsoft Store Python stub.

## Owner decisions: do not ask again

The owner has explicitly authorized all of the following:

- download/build dependencies, build tools, and a hash-complete wheelhouse;
- install required tools and Windows components;
- unavoidable Windows-managed writes on `C:` (profiles, registry, drivers,
  installer metadata, WebView2, and system temporary components);
- use the real Deezer and SoundCloud accounts through interactive DeckPipe/UI
  login;
- use, back up, and mutate the current live Rekordbox library; the owner accepts
  that they can recreate it if necessary;
- logout/login, restart, uninstall/reinstall, and an elevation prompt when a
  named gate needs them;
- the normal Windows unknown-publisher/SmartScreen warning for an unsigned app.

The owner will not provide:

- a SignTool path, signing certificate, certificate thumbprint, or timestamp
  service;
- separate Deezer or SoundCloud test accounts.

Therefore:

- do not search certificate stores, install signing tools just to sign, create a
  self-signed certificate, or treat absence of signing as a blocker;
- signing and timestamp status must be `WAIVED_BY_OWNER`, never `PASS`;
- output channel is `private-beta`, never `public`;
- an unavailable official Codex Security scanner is also
  `WAIVED_BY_OWNER` after mandatory local hostile tests/manual review pass;
- no further approval is required before the gated live Rekordbox write after
  backup/dry-run preconditions pass.

These permissions do not waive the cheap controls protecting accounts and the
music library: auth/origin/CSP, DPAPI, redaction, exact source provenance,
hash locks, offline wheelhouse, SHA-256, SPDX, package allowlist, transaction,
backup/reconcile/integrity/restore, and process/orphan tests all remain hard
gates.

## Storage and host boundary

All paths the workflow can choose must be below:

`D:\DeckPipe-RC-Lab`

Create/use exactly:

- `downloads`
- `wheelhouse`
- `tool-cache`
- `build`
- `staging`
- `install`
- `backups\rekordbox`
- `qa-evidence`
- `logs-redacted`

Set build caches (`CARGO_HOME`, `RUSTUP_HOME`, `npm_config_cache`,
`PIP_CACHE_DIR`, task `TEMP`, and task `TMP`) under `tool-cache`. Do not choose a
payload/cache on `C:`. OS-managed `C:` writes are accepted and should be noted,
not treated as a failure.

Do not install Docker for this closure. It cannot prove native Windows
Tauri/WebView2, NSIS/MSI, profile, logout/login, or Rekordbox behavior. The
verified 2026-08-28 inventory found no Docker/Podman/VirtualBox/VMware/Multipass,
no WSL distro, and no usable managed Hyper-V service. A dedicated local Windows
test account is the approved clean environment.

Previously verified host facts, all of which must be revalidated before use:

- Windows 11 Pro x64, build `10.0.26200`;
- D: had about 139.5 GB free of 238.5 GB;
- Rekordbox `7.2.14.0323` at
  `F:\rekordbox 7.2.14\rekordbox.exe`;
- live DB at
  `C:\Users\peche\AppData\Roaming\Pioneer\rekordbox\master.db`;
- live DB size was 10,211,328 bytes, last write 2026-08-26 19:40:57;
- Rekordbox was not running during inventory;
- stale DeckPipe 0.5.0 was observed at `E:\DeckPipe\deckpipe.exe`.

Inventory facts are not mutation authority or current evidence. Recheck process,
file identity, size, time, and SHA-256 immediately before copying/writing.

## Secrets and external-state rules

- Never inspect, copy, export, print, or log cookies, browser profiles,
  credential databases, ARL values, OAuth/refresh tokens, or DPAPI plaintext.
- Use DeckPipe's own interactive provider login windows. Existing browser login
  is only a convenience for the owner; it is not permission to extract browser
  state.
- If a provider asks for login or MFA, leave that visible interaction to the
  owner and state the exact next click/check. Continue after they confirm.
- Record account-independent evidence only: provider, flow, aggregate count,
  status, candidate hash, and redacted error class. No account names, titles,
  URLs, IDs, or user paths.
- Use uniquely prefixed minimal test objects. Avoid purchases, subscriptions,
  account-security changes, bulk library actions, and remote deletions.
- Test media may exist only under the D lab and should be safely removed after
  non-content evidence is recorded.
- If a secret-shaped value appears in output, stop, do not echo it again, remove
  it from evidence, rotate only if the owner explicitly requests rotation, and
  record a redacted security failure.

## What remains and why

| Order | Remaining work | Why it is required |
|---:|---|---|
| 1 | Task 9: machine-readable private-beta policy and verifier behavior | Prevents an owner waiver from becoming a fake signing pass or silently authorizing a future public build. |
| 2 | Task 10: hash-complete locks and D-drive offline wheelhouse | The current build intentionally fails closed; this makes the Python/PyInstaller input repeatable and auditable. |
| 3 | Task 11: real Windows Tauri/NSIS/MSI build and immutable candidate pin | Source tests do not prove that the native package builds or that later tests use the same bytes. |
| 4 | Task 12: clean Unicode Windows profile lifecycle | Proves first run, upgrade/migration, logout/login, uninstall/reinstall, WebView2, and orphan cleanup outside the developer profile. |
| 5 | Task 13: live Deezer/SoundCloud flows | Mocks do not prove provider login, network responses, downloads, error handling, or DPAPI persistence. |
| 6 | Task 14: live Rekordbox transaction and restore | Fakes do not prove compatibility with the installed 7.2.14 schema/files; backup and restore evidence protects diagnosability. |
| 7 | Task 15: full regression, security review, final RC audit | Catches cross-surface regressions and produces one honest go/no-go record bound to exact source/artifact hashes. |

Execute them strictly in order. Do not start clean-profile, account, or Rekordbox
acceptance until `release/verify.ps1` returns `PASS` for one frozen candidate and
`candidate-pin.json` has been written. Do not rebuild between gates without
invalidating and restarting candidate-bound evidence.

## Exact execution checkpoints

### 1. Policy contract

Implement Task 9 with RED/GREEN tests. Private-beta build mode is explicit
`-PrivateBetaCandidate`. Legacy `-UnsignedEngineeringCandidate` remains
`BLOCKED`; normal installed QA accepts only verifier-confirmed `PASS`. Policy is
tracked and staged; callers cannot choose a policy path/channel. Artifact names
contain `unsigned-private-beta`.

Commit: `build(release): encode private beta waiver policy`

### 2. Locks and wheelhouse

Implement Task 10 and run the D-rooted preparation command from the plan using
`pip-tools==7.6.1`. Both locks require real hashes. Prove a fresh install using
only `--no-index --find-links --require-hashes`; bind the sorted wheelhouse
manifest into release evidence. Never commit external wheels/venvs.

Commit: `build(deps): lock Windows wheelhouse with hashes`

### 3. Native build and candidate freeze

Build from a clean tracked `HEAD` into:

`D:\DeckPipe-RC-Lab\staging\deckpipe-0.6.0-private-beta`

Use `-PrivateBetaCandidate`; pass no signing inputs. Require verifier JSON
`PASS`/exit 0, `WAIVED_BY_OWNER` for signing/timestamp, exact SHA-256/SPDX/
allowlist/provenance, and `NotSigned` top-level packages. Write:

`D:\DeckPipe-RC-Lab\qa-evidence\candidate-pin.json`

Candidate identity is immutable after this point. If the stage changes, discard
its evidence and rebuild from a clean commit.

### 4. Clean Unicode profile

Create a new standard local account such as `DeckPipe_Тест_0828` only if that
exact name does not exist. Enter its password through `Read-Host -AsSecureString`;
never put it in a command or file. Install payloads on D. Acknowledge the
unknown-publisher warning, run normal installed QA without
`-AllowUnsignedEngineeringEvidence`, then verify first run, restart,
logout/login, crash/recovery, portable 0.5.0 migration or truthful
`NOT_APPLICABLE` MSI upgrade, uninstall/reinstall, and no owned process/listener.
Disable but do not automatically delete the account/profile after all gates.

Before logout/reboot, write a checkpoint under `qa-evidence`, commit code, tell
the owner the exact gate and next command, and say that they must reopen/continue
the task. Never promise an automatic return without an actual wait/schedule
mechanism.

### 5. Live accounts

Use the installed candidate. First prove login-cancel UX, then have the owner
complete DeckPipe's Deezer and SoundCloud login windows if requested. Exercise
the bounded flows in Task 13, including one selected sample per provider,
generic error handling, restart/logout persistence, and output redaction. Do not
mutate remote state that cannot be cleaned up. Track only the redacted audit:

`audit/deckpipe-private-beta-live-accounts-2026-08-28.md`

### 6. Live Rekordbox

Implement the tested explicit D-drive backup-root contract and the new live
runner. Keep the old Unicode runner read-only. Immediately before testing:

1. revalidate exact Rekordbox executable/DB and prove no Rekordbox process;
2. inventory DB plus adapter-declared ANLZ/XML/external files;
3. copy/hash the full inventory to a timestamped D backup;
4. run integrity/snapshot and dry-run;
5. use `DECKPIPE_RB_EXPERIMENTAL=1`, the exact confirmation phrase, and a unique
   playlist whose name begins `DECKPIPE_RC_20260828_`;
6. apply, reopen, reconcile, and integrity-check;
7. close handles, restore the pretest backup, reopen, and prove the full original
   snapshot/hash inventory and no test playlist.

No extra owner approval is needed. If restore verification fails, stop using
Rekordbox, preserve both copies, and report the exact failure; do not conceal it
because recreation is possible.

Track only redacted evidence:

`audit/deckpipe-private-beta-rekordbox-acceptance-2026-08-28.md`

### 7. Whole-branch closure

Run every command in Task 15, the real verifier, installed runner, live account
matrix, Rekordbox matrix, and final candidate rehash. Run the official Codex
Security standard/diff/candidate scans only if the scanner is already callable;
otherwise record `WAIVED_BY_OWNER` and run all mandatory local hostile/manual
checks. Request one read-only whole-branch review and permit at most one bounded
P0/P1 or release-contract fix round with TDD.

Write:

`audit/deckpipe-private-beta-rc-verification-2026-08-28.md`

Do not modify:

`audit/deckpipe-release-hardening-verification-2026-08-27.md`

## Interaction boundaries

Continue autonomously through downloads, tool installs on D, source changes,
builds, local tests, installer invocations, live Rekordbox backup/write/restore,
and redacted evidence. Ask the owner only when the next physical/UI action
requires one of:

1. UAC/elevation confirmation;
2. provider login or MFA inside the visible DeckPipe window;
3. logout/login or reboot;
4. an unexpected external-state choice materially beyond the approved flows.

For each pause, say exactly:

- which gate is waiting;
- what the owner must do;
- what must remain open/closed;
- the exact resume check/command;
- whether the current candidate/evidence remains valid.

Do not ask again about signing, test accounts, Docker, D-vs-C policy, official
scanner availability, or permission to mutate the backed-up Rekordbox library.

## Success and blocked definitions

The session is complete only when:

- policy/waiver tests prevent escalation to public/signed claims;
- both Python locks and the external wheelhouse are hash-complete and proven
  offline;
- a real Windows private-beta candidate exists and its verifier returns `PASS`;
- every artifact/evidence/SBOM/manifest/provenance hash is pinned;
- clean-profile lifecycle, installed security/performance, logout/login,
  upgrade/migration, uninstall/reinstall, and orphan checks pass;
- real Deezer and SoundCloud acceptance passes with redacted evidence;
- live Rekordbox dry-run/apply/reconcile/integrity/restore passes and the original
  library is restored;
- the full suite and independent review are clean;
- final candidate hashes still equal `candidate-pin.json`;
- the final audit calls the result exactly **DeckPipe 0.6.0 unsigned private
  beta**.

Signing and timestamping are successful owner waivers, not blockers and not
passes. An unavailable official scanner is also an owner waiver after local
security closure.

Use `BLOCKED` only for a real pending interaction/external outage or a failed
hard gate that cannot be safely fixed in the bounded round. Do not mark the task
complete because source tests are green, time is long, or a logout interrupts
the current process. Persist through safe in-scope work and leave an exact
checkpoint for any unavoidable user handoff.

## Final report to the owner

Lead with the outcome and include:

- exact source commit;
- exact staging/install paths on D;
- artifact names, sizes, and SHA-256 values;
- final verifier status and candidate rehash;
- test counts and native/installed/live gate results;
- `WAIVED_BY_OWNER` rows for signing/timestamp and, only if unavailable, the
  official scanner;
- expected unknown-publisher warning;
- test Windows account state;
- confirmation that no credentials were exported/logged;
- Rekordbox backup path and verified restore result;
- any single remaining owner action/blocker.

Never describe this artifact as signed, SmartScreen-trusted, public,
production-wide, or generally release-ready.
