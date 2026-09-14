//! Unshipped, synthetic WebView2 persistence proof. No production URL override.
//! auth_webview_probe <write|read|clear> --local-app-data <absolute lab path>
//!   --provider <deezer|sc> --url <http://127.0.0.1:port/path> --result <json>
use deckpipe::auth_browser::{clear_browser_data, persistent_window, profile_directory};
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tauri::WebviewWindow;

async fn inspect(window: &WebviewWindow, expected: &tauri::Url) -> Result<bool, &'static str> {
    let expected = serde_json::to_string(expected.as_str()).unwrap();
    for _ in 0..100 {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let tx = std::sync::Mutex::new(Some(tx));
        // Only synthetic presence booleans cross the callback. No page content,
        // credential extraction script, or synthetic values enter the report.
        window.eval_with_callback(format!("(() => ({{ready: location.href === {expected} && document.readyState === 'complete', present: localStorage.getItem('deckpipe_probe') !== null}}))()"), move |value| {
            if let Some(tx) = tx.lock().unwrap().take() { let _ = tx.send(value); }
        }).map_err(|_| "PROBE_EVAL_FAILED")?;
        let value = tokio::time::timeout(Duration::from_secs(3), rx)
            .await
            .map_err(|_| "PROBE_EVAL_TIMEOUT")?
            .map_err(|_| "PROBE_EVAL_FAILED")?;
        if let Ok(value) = serde_json::from_str::<Value>(&value) {
            if value["ready"] == true {
                return Ok(value["present"] == true);
            }
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err("PROBE_PAGE_TIMEOUT")
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let mode = args.first().expect("mode required").clone();
    assert!(matches!(mode.as_str(), "write" | "read" | "clear"));
    let arg = |key: &str| {
        args.windows(2)
            .find(|p| p[0] == key)
            .expect("argument missing")[1]
            .clone()
    };
    let root = PathBuf::from(arg("--local-app-data"));
    assert!(root.is_absolute());
    assert_eq!(
        std::env::var_os("LOCALAPPDATA").map(PathBuf::from),
        Some(root.clone()),
        "child LOCALAPPDATA must equal explicit isolated root"
    );
    let provider = arg("--provider");
    let profile = profile_directory(&root, &provider).unwrap();
    let url: tauri::Url = arg("--url").parse().unwrap();
    assert_eq!(url.scheme(), "http");
    assert_eq!(url.host_str(), Some("127.0.0.1"));
    assert!(url.port().is_some());
    let result_path = PathBuf::from(arg("--result"));
    assert!(result_path.is_absolute());
    let app = tauri::Builder::default().setup(move |app| {
        let app = app.handle().clone();
        tauri::async_runtime::spawn(async move {
            let origin = url.origin();
            let policy = Arc::new(move |u: &tauri::Url| u.as_str() == "about:blank" || u.origin() == origin);
            let initial = if mode == "clear" { "about:blank".parse().unwrap() } else { url.clone() };
            let outcome = async {
                let window = persistent_window(app.clone(), "auth-probe".into(), profile, initial, policy, Arc::new(|| {}), false).await?;
                if mode == "clear" {
                    clear_browser_data(&window).await?;
                    window.navigate(url.clone()).map_err(|_| "PROBE_NAVIGATION_FAILED")?;
                }
                let storage = inspect(&window, &url).await?;
                let cookie_url = url.clone();
                let cookies = tokio::task::spawn_blocking(move || window.cookies_for_url(cookie_url)).await.map_err(|_| "PROBE_COOKIES_FAILED")?.map_err(|_| "PROBE_COOKIES_FAILED")?;
                Ok::<_, &'static str>(json!({"ok":true,"mode":mode,"provider":provider,"cookie_present":cookies.iter().any(|c| c.name() == "deckpipe_probe"),"local_storage_present":storage}))
            }.await;
            let exit = if outcome.is_ok() {0} else {1};
            let value = outcome.unwrap_or_else(|code| json!({"ok":false,"errorCode":code}));
            let written = std::fs::write(result_path, serde_json::to_vec_pretty(&value).unwrap());
            app.exit(if written.is_ok() {exit} else {2});
        });
        Ok(())
    }).build(tauri::generate_context!()).expect("probe setup failed");
    app.run(|_, _| {});
}
