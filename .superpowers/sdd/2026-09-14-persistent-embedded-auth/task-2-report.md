# Task 2 — Interface and extension-free Windows packaging

Status: READY_FOR_CONTROLLER_COMMIT. No commit, push, installation, registration, live account/profile access, or candidate release build was performed.

## Scope and affected files

Changed:

- `frontend/app.js`, `frontend/index.html`: embedded login copy and controls; no helper action; recoverable-status rendering; retry; explicit logout/account switch; epoch-based stale-response protection.
- `app/static/{app.js,index.html}` and `desktop/ui/{app.js,index.html}`: regenerated only with `node frontend/build.mjs`.
- `desktop/src-tauri/tauri.conf.json`: keeps the backend sidecar and removes extension/helper resources from bundle configuration.
- `release/build.ps1`: removes exported-source helper build/bootstrap and host-output assertion.
- `README.md`, `BUILD.md`: replace current helper/Firefox instructions with DeckPipe-owned embedded-session instructions and current checks.
- `qa/tests/test_stage_ab_frontend.py`: real FakeDOM behavior checks for popup notice, retry, cancellation/polling stop, stale response suppression, and ordered account switch.
- `qa/tests/test_embedded_auth_package.py`: package configuration, explicit tracked-inventory, and current documentation assertions.
- `qa/tests/test_desktop_contract.py`, `qa/tests/test_frontend_build_contract.py`: expect the four-command embedded contract and reject `auth_open_setup`.

Removed through an explicit tracked-file inventory:

- `extension/background.js`, `extension/helper-core.js`, `extension/manifest.json`, `extension/popup.css`, `extension/popup.html`, `extension/popup.js`
- `release/auth-helper/Build-AuthHelper.ps1`, `README.md`, `Register-DeckPipeAuthHost.ps1`, `Unregister-DeckPipeAuthHost.ps1`, `package_helper.py`
- obsolete helper/extension tests: `qa/tests/auth_helper.test.cjs`, `DeckPipe.AuthSetup.Tests.ps1`, `test_auth_helper_package.py`, `test_extension_contract.py`

No recursive deletion was used. The explicit removals are staged by `git rm` for the controller's commit.

## UI behavior and race rationale

`openLogin` and `tauriLogin` assign an attempt epoch before awaiting native work. A late `auth_status(null)` cannot reopen a closed dialog or replace a newer provider dialog; a late `auth_begin` response after cancel/retry/provider switch is rejected and, if it created a request late, cancelled without rendering its state. Status-poll callbacks capture both request ID and epoch, so cancellation clears the timer and a queued callback cannot invoke `auth_status` or show a stale connected account.

`AUTH_POPUP_BLOCKED` remains a `waiting_browser` state and displays: popup sign-in is unavailable; continue on the service page in DeckPipe's window. It is not labelled a connection failure and the normal waiting poll stays active. `AUTH_PROVIDER_REJECTED` explains logging in again in the same window. `AUTH_BACKEND_UNAVAILABLE` and `AUTH_INVALID_RESPONSE` show an enabled retry action, which starts a fresh `auth_begin` attempt even if unchanged cookie material is deduplicated by native code. `validating` replaces waiting notices normally.

The connected-state controls read exactly `Выйти и забыть вход` and `Сменить аккаунт`. Switching runs selected-provider `auth_logout` before a new `auth_begin`; if cancel/logout fails, the UI visibly says it could not forget the selected sign-in and does not start a replacement attempt. No profile location, token, or cookie is shown to the user.

## Red / green evidence

RED before implementation:

- `test_stage_ab_frontend.py`: 4 new behavior tests failed for missing popup notice/retry/account-switch functions and stale-response protection.
- `test_embedded_auth_package.py`: 3 assertions failed because helper resources, tracked extension/helper inventory, and current Firefox/helper docs still existed.
- Follow-up race/failure RED: late `openLogin` status reopened a newer dialog and a failing `auth_logout` escaped without an honest UI result.

GREEN after implementation (environment used synthetic `DECKPIPE_DATA_DIR`, `DECKPIPE_API_TOKEN=synthetic-test-only`, `DECKPIPE_BOUND_PORT=7100`, `PYTHONDONTWRITEBYTECODE=1` and the prescribed offline Python):

- `qa/tests/test_frontend_contract.py`: 11 passed.
- `qa/tests/test_stage_ab_frontend.py`: 19 passed.
- `qa/tests/test_desktop_contract.py` with `PYTHONPATH=.`: 9 passed.
- `qa/tests/test_embedded_auth_package.py`: 3 passed.
- `powershell.exe -NoProfile -File qa/tests/DeckPipe.QA.Tests.ps1`: 13 passed.
- `powershell.exe -NoProfile -File qa/tests/DeckPipe.Release.Tests.ps1`: 37 passed.
- `powershell.exe -NoProfile -File qa/tests/DeckPipe.Orchestrator.Tests.ps1`: 12 passed.
- `git diff --check`: clean after the controller corrected its unrelated plan trailing blank line.

## Generated assets and remaining boundary

`node frontend/build.mjs` completed successfully and regenerated both tracked UI destinations. `qa/tests/test_frontend_build_contract.py` currently reports exactly one expected failure: `test_generated_asset_blobs_are_archive_stable_on_windows` compares generated output against `HEAD`/`git archive HEAD`, which still has the old assets. The other 14 tests pass. This archive contract was not weakened.

Controller must commit the Task 1 + Task 2 source and regenerated assets, then rerun the full frontend archive contract and its final integration/candidate-build checks against that exact revision. A real Deezer/SoundCloud login, 2FA/CAPTCHA, persistent-profile UX, installer contents, and actual candidate build remain external/controller acceptance boundaries.
