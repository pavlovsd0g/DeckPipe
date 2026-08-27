# DeckPipe 0.6.0 private-beta RC closure design

**Date:** 2026-08-28  
**Status:** Owner-approved in conversation; written review pending  
**Implementation baseline:** `d261c79ea556c44c7a17996595a381fe4f5eb69c` on `codex/release-hardening`  
**Supersedes for future RC work:** the signing, clean-machine, test-account, and disposable-Rekordbox assumptions in the 2026-08-27 plan and handoff. Historical evidence remains unchanged.

## 1. Objective

Close the remaining release-candidate gates for DeckPipe 0.6.0 and produce an
installable **unsigned private-beta** package intended for a small, controlled
group (currently expected to be fewer than 50 users).

This is not a public, reputation-backed Windows release. Users may see Windows
SmartScreen or unknown-publisher warnings. The owner explicitly accepts that
risk. The package must still be reproducible, have exact checksums and an SPDX
SBOM, exclude credentials and unexpected files, survive lifecycle testing, and
prove its Rekordbox and account integrations on the actual host environment.

## 2. Current verified state

- Source/synthetic/offline hardening is complete through `ff79898`; the final
  verification audit is committed at `d261c79`.
- The current release worktree is
  `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`.
- The primary checkout remains intentionally untouched because it has existing
  user-owned modifications in two PowerShell QA files.
- Windows host: Windows 11 Pro x64, build family `10.0.26200`.
- `D:` has approximately 139.5 GB free at design time.
- Docker, Podman, VirtualBox, VMware, and Multipass are not installed.
- WSL exists but has no installed distribution and cannot validate the Windows
  GUI installer, Tauri lifecycle, or Rekordbox integration.
- A hypervisor is present at the OS level, but Hyper-V management cmdlets and
  the VMMS service are unavailable. No usable existing Windows VM runtime was
  found.
- Rekordbox 7.2.14 is installed at `F:\rekordbox 7.2.14\rekordbox.exe`.
- The current Rekordbox database is
  `C:\Users\peche\AppData\Roaming\Pioneer\rekordbox\master.db`.
- Rekordbox was not running during the inventory.

## 3. Distribution policy

### 3.1 Release channel

The supported output of this closure is `private-beta`, not `public`.

Add a tracked machine-readable policy, `release/policy.json`, with at least:

- schema version;
- channel `private-beta`;
- signing requirement `owner-waived`;
- timestamp requirement `owner-waived`;
- Windows reputation warning `accepted`;
- waiver date `2026-08-28`;
- intended audience statement `controlled-small-group`.

The exact user count is context, not an enforceable runtime limit.

### 3.2 Unsigned evidence semantics

Signing and timestamping must never be reported as `PASS`. For this channel,
release evidence records both as `WAIVED_BY_OWNER` and references the tracked
policy. The verifier may return overall `PASS` for an unsigned private-beta
candidate only when:

1. the tracked policy is exact and valid;
2. the artifact is clearly identified as private-beta/unsigned;
3. every non-waived artifact, provenance, checksum, SBOM, allowlist, build,
   install, and runtime gate passes;
4. neither evidence nor command-line input can turn the waiver into permission
   for a `public` channel.

A future public release must change the policy and restore mandatory valid
Authenticode plus RFC3161 timestamp evidence.

### 3.3 Mandatory low-cost protections

Small distribution size does not waive controls that protect accounts or music
libraries. The following remain mandatory:

- loopback/auth/origin/CSP and hostile-input regression tests;
- DPAPI credential storage and secret-output scans;
- exact tracked-source provenance;
- hash-complete Python locks and an offline wheelhouse;
- final SHA-256 manifest;
- deterministic SPDX 2.3 SBOM;
- closed package allowlist with no credentials/config/database/media files;
- transactional Rekordbox backup, reconciliation, and integrity checks;
- installed-process ownership and orphan cleanup checks.

The official Codex Security plugin scan becomes best-effort. If the scanner is
still unavailable, record `WAIVED_BY_OWNER` with the existing local hostile
test and manual-review evidence; do not weaken the source security tests.

## 4. Host and storage policy

### 4.1 Lab root

All paths that the workflow can explicitly choose must live below:

`D:\DeckPipe-RC-Lab`

Suggested layout:

```text
D:\DeckPipe-RC-Lab\
  downloads\
  wheelhouse\
  tool-cache\
  build\
  staging\
  install\
  backups\rekordbox\
  qa-evidence\
  logs-redacted\
```

Do not repurpose the repository, its worktree, or the preserved 0.5.0 backup as
scratch storage.

### 4.2 C-drive boundary

Do not deliberately select `C:` for downloaded installers, package caches,
build outputs, VM images, lab data, or application installation when a path is
configurable. The owner accepts unavoidable Windows-managed writes such as
registry entries, drivers, system components, temporary files, and the local
test-user profile.

If a proposed virtualization/container product requires its main payload,
images, or mutable cache on `C:`, do not install it. Stop and use the fallback
below.

### 4.3 Clean environment strategy

Do not install Docker for this gate. Linux containers cannot prove Windows GUI
installer, Tauri, reboot/login, or Rekordbox behavior.

Use a dedicated clean local Windows test user on the current host. Prefer a
non-ASCII user name so the same profile covers both clean-profile and Unicode
path behavior. Install DeckPipe under `D:\DeckPipe-RC-Lab\install` when the
installer supports a selectable path. The profile itself may be created under
Windows-managed `C:\Users`.

Virtualization is optional, not a prerequisite. A future VM may be created only
after a read-only inventory proves that its main program, image, and cache can
be kept on `D:`. Do not enable or install a hypervisor merely to satisfy a label
when the dedicated-host-profile matrix covers the private-beta risk.

## 5. Dependency and build closure

The owner authorizes downloading build/runtime dependencies and installing
required build tools, subject to the `D:\DeckPipe-RC-Lab` storage policy.

The next session must:

1. inventory the actual Python, Node, Rust, Tauri, and Windows packaging tools;
2. resolve every direct and transitive Python runtime/build distribution for
   the Windows target;
3. store the distributions in the D-drive wheelhouse;
4. generate real hashes in `requirements.lock` and
   `requirements-build.lock` without inventing unavailable artifacts;
5. prove a clean offline install with `--no-index --require-hashes`;
6. run the existing release-contract tests;
7. build from the exact tracked `HEAD` in the isolated build workspace;
8. produce the application/setup/MSI artifact set required by the release
   contract, or narrow that set in code and tests if the actual Tauri toolchain
   legitimately produces a different private-beta set;
9. generate release evidence, SHA-256 manifest, SPDX SBOM, and package
   inventory from the final candidate bytes;
10. verify atomic staging and preserve all failure evidence outside the final
    publish directory.

Dependency downloads are not user testing and require no additional approval.
Do not access unrelated credential stores while resolving packages.

## 6. Installed lifecycle verification

Against one exact candidate hash, execute and record:

1. clean-profile install and first launch;
2. backend readiness and packaged-origin authentication;
3. restart and logout/login behavior;
4. upgrade from the preserved 0.5.0 artifact where its packaging format permits
   a meaningful upgrade path;
5. uninstall cleanup;
6. reinstall;
7. non-ASCII profile/path behavior;
8. crash/restart recovery;
9. sidecar termination and no orphan process;
10. installed hostile origin/auth/XSS/extension reproducers;
11. installed performance comparison;
12. final package hash recheck after all testing.

If the preserved 0.5.0 artifact is portable rather than installable, document
and test the actual supported migration scenario instead of fabricating an MSI
upgrade claim.

## 7. Real account authorization

The owner authorizes testing with the Deezer and SoundCloud accounts already
logged in through the local browser/UI. Test accounts will not be supplied.

Rules:

- use interactive browser/application sessions; never export cookies, refresh
  tokens, browser profiles, or credential databases;
- never print or copy account secrets into commands, source, reports, or logs;
- if authentication or MFA is required, pause at that exact boundary and ask
  the owner to complete it interactively;
- use uniquely prefixed, minimal, reversible test objects where external state
  must be created;
- avoid purchase, subscription, account-security, deletion, or bulk-library
  actions;
- clean up test objects where the service permits safe cleanup;
- record only redacted account-independent evidence.

Real network acceptance must cover useful application flows and generic failure
handling, not merely a successful login.

## 8. Rekordbox authorization and safety

The owner explicitly authorizes use and mutation of the current Rekordbox
database and accepts that it can be recreated if necessary. No further approval
is required before a gated test write.

The workflow still creates a backup because backup/restore is part of the
product contract and makes failures diagnosable:

1. prove Rekordbox is closed and refuse the write if it is running;
2. inventory the database plus every adapter-declared ANLZ/XML/external file
   that may be affected;
3. copy that inventory to a timestamped directory below
   `D:\DeckPipe-RC-Lab\backups\rekordbox`;
4. record original sizes and SHA-256 values;
5. run a read-only integrity/snapshot check;
6. run dry-run and inspect the exact proposed operations;
7. create/use a uniquely prefixed test playlist and perform the explicitly
   gated apply;
8. close/reopen, reconcile exact order and metadata, and run integrity checks;
9. exercise Cyrillic, emoji, NFC/NFD, and relevant path cases;
10. test intentional failure/rollback primarily against the backed-up working
    copy when fault injection could deliberately corrupt external state;
11. clean the test playlist when safe, or restore the verified backup if
    cleanup/reconciliation fails.

The installed Rekordbox application directory alone is not treated as the
library. The database and external analysis/XML inventory are the integrity
boundary.

## 9. Failure and interaction policy

- Keep the first pass read-only until paths, free space, tool versions, process
  state, and exact mutation targets are recorded.
- Do not silently install a tool whose selected main path is `C:`.
- Do not claim signing, timestamping, deep-plugin scanning, clean-machine VM,
  or public reputation evidence when those controls are waived.
- Stop after three failures of the same technical approach and preserve the
  evidence instead of retrying recursively.
- Ask the owner only for interactive login/MFA, a Windows elevation prompt, or
  a genuinely unavoidable C-drive/system change not covered by this design.
- A reboot/logout request must name the pending gate and resume point before it
  happens; do not promise an unscheduled return.

## 10. Completion criteria

The private beta is complete only when:

- release policy and verifier tests distinguish private-beta waiver from public
  signing requirements;
- real hash-complete locks and the D-drive wheelhouse pass offline recreation;
- one exact unsigned candidate is built and atomically staged;
- SHA-256, SPDX, allowlist, provenance, and release evidence all pass;
- clean-profile lifecycle, installed security, performance, and orphan checks
  pass;
- authorized real account flows pass or have exact service-side blockers that
  the owner explicitly accepts;
- Rekordbox dry-run/apply/reconcile/Unicode/integrity/rollback evidence passes;
- an independent final review reports no unresolved P0/P1;
- the final audit calls the output `unsigned private beta`, documents every
  waiver and Windows warning, and never calls it a signed/public release.

## 11. Planned documentation outputs

After written design approval:

1. update
   `docs/superpowers/plans/2026-08-27-deckpipe-release-hardening.md` with a
   clearly dated owner-approved RC closure addendum rather than rewriting the
   historical task evidence;
2. create a fresh execution handoff at
   `audit/deckpipe-private-beta-rc-handoff-2026-08-28.md`;
3. leave the 2026-08-27 verification audit unchanged as a historical snapshot;
4. instruct the next session to produce a new final RC audit after execution.

