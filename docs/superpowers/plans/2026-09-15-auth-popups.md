# Embedded authentication popups — Stage D correction

The owner reproduced a failed Google login from SoundCloud and explicitly requested support for authentication popups. The installed candidate `ef2e0c6` denied every `window.open` request. This correction stays inside the approved persistent DeckPipe browser design.

## Design and acceptance

- Create a real child WebView2 using Tauri `NewWindowResponse::Create` and the opener's `window_features`; preserve the shared environment, browser profile and `window.opener`/`postMessage` relationship.
- Permit the initial `about:blank` used by identity providers, then enforce the existing navigation policy on each child. Keep the packaged IPC capability restricted to `main`. No provider credential values or page contents enter diagnostics.
- Scope children to one attempt, limit concurrent children to four, release slots when they close, and remove children when their parent is cancelled, expires, succeeds or is replaced. Closing a child does not cancel the parent attempt.
- WebView2's page-requested close must also destroy the outer Tauri window. The initial integration probe found that Wry only destroyed its child HWND, leaving an empty outer window and occupied slot.
- Exercise the production factory in an unshipped native probe using two synthetic loopback origins: blank window, identity-provider redirect, shared cookie/storage, opener handoff, six sequential openings and page closes, denied navigation, four-child limit and parent cleanup. Existing broker cancellation/expiry tests and the full source gate remain required.
- Rebuild and pin a new immutable Windows candidate, upgrade while preserving user data, then let the owner perform Google authentication in the real service. Synthetic results do not establish live Google/SoundCloud acceptance.

The original native probe failed with `PROBE_POPUP_HANDOFF_FAILED`. The expanded corrected probe passed at `D:/DeckPipe-RC-Lab/qa-evidence/windows-stage-d-20260915/popup-regression/result.json`. Independent review found no remaining substantial implementation issue. A precisely forced parent-close-during-construction interleaving has not been separately demonstrated; the implementation checks the closed attempt both before reservation and after window creation.

References: [Tauri popup API](https://docs.rs/tauri/2.11.5/tauri/webview/struct.WebviewWindowBuilder.html#method.on_new_window), [WebView2 shared environment requirement](https://learn.microsoft.com/en-us/dotnet/api/microsoft.web.webview2.core.corewebview2newwindowrequestedeventargs.newwindow). The pinned Wry0.55.1 implementation defers its callback to avoid WebView2 reentrancy.
