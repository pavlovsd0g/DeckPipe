# Task 2B implementer report — bundled UI and supervised sidecar

## Scope

Implemented Task 2B in `D:\Claude Code\Projects\deezer-rekordbox-sync\.worktrees\release-hardening`.

Owned Task 2B files changed:

- `desktop/package.json`
- `desktop/src-tauri/Cargo.toml`
- `desktop/src-tauri/Cargo.lock`
- `desktop/src-tauri/src/main.rs`
- `desktop/src-tauri/tauri.conf.json`
- `desktop/src-tauri/capabilities/default.json`
- `desktop/src-tauri/permissions/backend-connection.toml`
- `run_backend.py`
- `dev.js`
- `qa/tests/test_desktop_contract.py`

Controller-approved mechanical generated schema files changed:

- `desktop/src-tauri/gen/schemas/acl-manifests.json`
- `desktop/src-tauri/gen/schemas/capabilities.json`
- `desktop/src-tauri/gen/schemas/desktop-schema.json`
- `desktop/src-tauri/gen/schemas/windows-schema.json`

No live profile data, credentials, external accounts, services, Rekordbox data, or ignored sidecar binary were read, copied, rebuilt, or replaced.

## Raw RED

The repository venv was required. The plain `python` launcher on this host is the Windows Store alias and emitted only `Python`, so RED/GREEN used:

`rtk 'D:\Claude Code\Projects\deezer-rekordbox-sync\.venv\Scripts\python.exe' -m unittest qa.tests.test_desktop_contract -v`

Initial Task 2B RED against current HEAD after adding the desktop contract test:

- `Ran 7 tests`
- `FAILED (failures=4, errors=2)`
- Errors:
  - `AttributeError: module 'run_backend' has no attribute 'create_bound_listener'`
  - `AttributeError: module 'run_backend' has no attribute 'start_parent_watchdog'`
- Failures:
  - capability still exposed remote/default scope instead of only custom main-window commands
  - Tauri config did not satisfy bundled UI / `withGlobalTauri=false` / strict CSP contract
  - `dev.js` did not launch `run_backend.py` with parent/token contract
  - Rust lifecycle contract was absent: bundled `WebviewUrl::App`, managed read-once backend connection, retained/killed sidecar, random launch token, exact port parse, single instance

Follow-up RED for controller-requested hardening before production changes:

`rtk 'D:\Claude Code\Projects\deezer-rekordbox-sync\.venv\Scripts\python.exe' -m unittest qa.tests.test_desktop_contract -v`

- `Ran 8 tests`
- `FAILED (failures=4)`
- Failing contracts:
  - missing `SO_EXCLUSIVEADDRUSE` and typed Win32 `OpenProcess` / wait / close signatures
  - desktop `npm run build` did not regenerate root frontend before `tauri build`
  - dev launcher still inherited `...process.env`
  - single-instance plugin was ordered after shell plugin

## Implementation summary

- Tauri now creates the main window from `WebviewUrl::App("index.html".into())`; `app.windows` is empty and `withGlobalTauri` is false.
- Added strict CSP and restrictive supported Tauri document headers without COEP, preserving external cover-image compatibility.
- Capability is limited to `windows: ["main"]` and custom permissions `allow-service-login` / `allow-backend-connection`; login windows receive no capability.
- Added `backend_connection` command guarded by invoking window label and read-once managed memory (`Mutex<Option<BackendConnection>>`).
- Desktop launch token is generated from `OsRng` as 32 random bytes and hex-encoded; token is only passed to the sidecar as `DECKPIPE_API_TOKEN` and stored in Tauri managed memory until first frontend read.
- Tauri sidecar launch uses `.env_clear()`, a Windows/runtime allowlist, `DECKPIPE_API_TOKEN`, `DECKPIPE_PARENT_PID`, and args `--host 127.0.0.1 --port 0`.
- Tauri retains `CommandChild` immediately, parses exactly one `DECKPIPE_PORT=<1..65535>` line under a 15s timeout, watches premature termination, and kills the sidecar on startup failure, duplicate retain, backend connection store failure, main-window build failure, and app exit.
- Single-instance plugin is registered before shell plugin; a second invocation focuses the existing `main` window and must not reach sidecar startup in setup.
- Backend binds a single loopback listener once, sets `SO_EXCLUSIVEADDRUSE` on Windows when available, never uses `SO_REUSEADDR`, prints only `DECKPIPE_PORT=<port>` after bind, and passes the same socket to `uvicorn.Server.run(sockets=[listener])`.
- Backend parent watchdog uses typed `ctypes.WinDLL("kernel32")` signatures with `wintypes` for `OpenProcess`, `WaitForSingleObject`, and `CloseHandle`; invalid/unopenable parent fails closed by setting `server.should_exit`.
- Uvicorn surface is reduced with `access_log=False`, `server_header=False`, `date_header=False`, and `proxy_headers=False`.
- Browser dev launcher uses `run_backend.py`, synthetic/current token via env only, Node PID as parent, and the same child env allowlist rather than inheriting all `process.env`.
- `desktop/package.json` build script now runs `npm --prefix .. run build:frontend && tauri build`, preventing stale `desktop/ui` packaging.

## Dependency provenance

Official Tauri documentation was used for:

- bundled app URL / `WebviewUrl::App`
- CSP/header support
- shell sidecar environment/stdout semantics
- `CommandChild` lifecycle
- single-instance plugin registration

Direct Rust dependencies added and pinned:

- `tauri-plugin-single-instance = "=2.4.3"` — official Tauri plugin from crates.io, selected via `cargo search tauri-plugin-single-instance --limit 5`
- `rand = "=0.8.5"` — crates.io, used only for OS random launch token bytes
- `hex = "=0.4.3"` — crates.io, used only to encode the launch token bytes

Resolved direct provenance checks:

```text
rtk cargo tree -i rand --locked
rand v0.8.5
└── deckpipe v0.5.0

rtk cargo tree -i hex --locked
hex v0.4.3
└── deckpipe v0.5.0

rtk cargo tree -i tauri-plugin-single-instance --locked
tauri-plugin-single-instance v2.4.3
└── deckpipe v0.5.0
```

All transitive versions are locked in `desktop/src-tauri/Cargo.lock`.

## Socket and watchdog smoke evidence

Covered by `qa/tests/test_desktop_contract.py`:

- real temporary loopback listener remains bound while a competing bind fails
- exact same listener object reaches `uvicorn.Server.run(sockets=[listener])`
- selected port is taken from the listener socket, not from a free-port probe
- no `SO_REUSEADDR`, no `free_port`, no `uvicorn.run`
- Windows exclusive bind and typed `kernel32` signatures are source-locked
- invalid parent handle sets `server.should_exit`
- fake parent exit sets `server.should_exit` and closes the parent handle
- subprocess smoke launches `run_backend.py --host 127.0.0.1 --port 0` with synthetic token/current parent, reads exactly one `DECKPIPE_PORT=<port>` line, verifies `/api/version` 200, `/api/jobs` unauthenticated 401 without token disclosure, `/api/jobs` authenticated 200 `[]`, then terminates cleanly

No live credentials or live profile directories were used; smoke tests used temporary `APPDATA` and `LOCALAPPDATA`.

## Verification

Passed:

```text
rtk 'D:\Claude Code\Projects\deezer-rekordbox-sync\.venv\Scripts\python.exe' -m unittest qa.tests.test_desktop_contract -v
Ran 8 tests in 0.988s
OK

rtk 'D:\Claude Code\Projects\deezer-rekordbox-sync\.venv\Scripts\python.exe' -m unittest qa.tests.test_security_contract qa.tests.test_frontend_build_contract -v
Ran 21 tests in 1.841s
OK

rtk 'D:\Claude Code\Projects\deezer-rekordbox-sync\.venv\Scripts\python.exe' -m unittest discover -s qa/tests -v
Ran 34 tests in 2.576s
OK (expected failures=1)

rtk 'D:\Claude Code\Projects\deezer-rekordbox-sync\.venv\Scripts\python.exe' -m compileall -q app run_backend.py qa/tests/test_desktop_contract.py
exit 0

rtk node --check dev.js
exit 0

rtk npm ci
added 3 packages; found 0 vulnerabilities

rtk npm run build:frontend
> node frontend/build.mjs
exit 0

rtk npm ci
working directory: desktop
added 2 packages; found 0 vulnerabilities

rtk npm run build
working directory: desktop
> npm --prefix .. run build:frontend && tauri build
Finished release build and produced MSI/NSIS bundles under ignored target paths

rtk cargo check --locked
Finished `dev` profile

rtk cargo test --locked
cargo test: 0 passed (1 suite, 0.00s)

rtk pwsh -NoProfile -ExecutionPolicy Bypass -File qa\tests\DeckPipe.QA.Tests.ps1
RESULT passed=9 failed=0

rtk pwsh -NoProfile -ExecutionPolicy Bypass -File qa\tests\DeckPipe.Orchestrator.Tests.ps1
RESULT passed=12 failed=0
```

Generated schema determinism:

```text
rtk git hash-object desktop/src-tauri/gen/schemas/acl-manifests.json desktop/src-tauri/gen/schemas/capabilities.json desktop/src-tauri/gen/schemas/desktop-schema.json desktop/src-tauri/gen/schemas/windows-schema.json
f863623c7cd224112c39f65dca8884e9b9615398
3460444eb2a3ed611adb348f129b8a1584554dfe
23647c20a94202eb816b39d5244be09051e47fca
23647c20a94202eb816b39d5244be09051e47fca

rtk cargo check --locked
Finished `dev` profile

rtk git hash-object desktop/src-tauri/gen/schemas/acl-manifests.json desktop/src-tauri/gen/schemas/capabilities.json desktop/src-tauri/gen/schemas/desktop-schema.json desktop/src-tauri/gen/schemas/windows-schema.json
f863623c7cd224112c39f65dca8884e9b9615398
3460444eb2a3ed611adb348f129b8a1584554dfe
23647c20a94202eb816b39d5244be09051e47fca
23647c20a94202eb816b39d5244be09051e47fca
```

Static forbidden-pattern audit:

```text
rtk rg -n "unsafe-inline|unsafe-eval|WebviewUrl::External\(url\.parse\(\)\.expect|unwrap_or\(7100\)|free_port|SO_REUSEADDR|uvicorn\.run\(|DECKPIPE_API_TOKEN.*print|print\(.*token|console\.(log|info|warn|error)\([^)]*token|__TAURI__|localStorage|sessionStorage|indexedDB|document\.cookie" desktop/src-tauri run_backend.py dev.js --glob "!desktop/src-tauri/gen/schemas/*" -S
exit 1, no matches
```

Blocked:

```text
rtk cargo fmt --check
error: 'cargo-fmt.exe' is not installed for the toolchain 'stable-x86_64-pc-windows-msvc'.
help: run `rustup component add rustfmt` to install it
```

I did not install Rust components because that would mutate the host toolchain outside the Task 2B scope.

## Self-review / CSO notes

- Token is not placed in sidecar args, stdout/stderr, URL/query/fragment, browser storage, disk, or error text.
- `backend_connection` returns the token only to `main`, only once, from managed memory.
- Child environment is cleared in Rust and allowlisted in Rust/dev; unrelated environment secrets are not forwarded.
- Sidecar output is parsed for the port line only; raw stdout/stderr is not surfaced to UI or error text.
- Single-bind listener closes the race between port discovery and Uvicorn startup; Windows exclusive-address-use is set before bind when available.
- Parent watchdog uses current-process-safe native wait on Windows and fails closed when the explicit parent PID cannot be opened.
- Capability has no remote block and no `core:default`; login windows have no permission scope.
- Existing `service_login` browser windows still navigate to Deezer/SoundCloud for the pre-existing credential flow. Credential storage behavior was intentionally not changed in Task 2B.

## Concerns

- `cargo fmt --check` is blocked until `rustfmt` is installed for `stable-x86_64-pc-windows-msvc`.
- Windows PowerShell 5.1 `-File` cannot parse the existing UTF-8 no-BOM PowerShell QA suites on this host; PowerShell 7 via `pwsh` passes both suites.
- `npm run build` in `desktop` produces release/bundle artifacts under ignored `target` paths. No ignored sidecar binary was rebuilt, copied, or replaced.
