//! Unshipped, synthetic WebView2 persistence proof. No production URL override.
//! auth_webview_probe <write|read|clear> --local-app-data <absolute lab path>
//!   --provider <deezer|sc> --url <http://127.0.0.1:port/path> --result <json>
use deckpipe::auth_browser::{clear_browser_data, persistent_window, profile_directory};
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tauri::{Manager, WebviewWindow};

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

async fn wait_probe(window: &WebviewWindow, field: &str) -> Result<(), &'static str> {
    let field = serde_json::to_string(field).unwrap();
    for _ in 0..100 {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let tx = std::sync::Mutex::new(Some(tx));
        window
            .eval_with_callback(
                format!("Boolean(window.probe && window.probe[{field}])"),
                move |value| {
                    if let Some(tx) = tx.lock().unwrap().take() {
                        let _ = tx.send(value);
                    }
                },
            )
            .map_err(|_| "PROBE_EVAL_FAILED")?;
        let value = tokio::time::timeout(Duration::from_secs(3), rx)
            .await
            .map_err(|_| "PROBE_EVAL_TIMEOUT")?
            .map_err(|_| "PROBE_EVAL_FAILED")?;
        if value == "true" {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err("PROBE_POPUP_HANDOFF_FAILED")
}

async fn wait_children(app: &tauri::AppHandle, count: usize) -> Result<(), &'static str> {
    for _ in 0..100 {
        if app
            .webview_windows()
            .into_values()
            .filter(|w| w.label().starts_with("auth-probe-popup-"))
            .count()
            == count
        {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    Err("PROBE_POPUP_COUNT_FAILED")
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let mode = args.first().expect("mode required").clone();
    assert!(matches!(
        mode.as_str(),
        "write" | "read" | "clear" | "popup"
    ));
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
            let mut idp = url.clone();
            idp.set_host(Some("localhost")).unwrap();
            let idp_origin = idp.origin();
            let is_popup = mode == "popup";
            let policy = Arc::new(move |u: &tauri::Url| u.as_str() == "about:blank" || u.origin() == origin || (is_popup && u.origin() == idp_origin));
            let initial = if mode == "clear" { "about:blank".parse().unwrap() } else { url.clone() };
            let outcome = async {
                let denied = Arc::new(std::sync::atomic::AtomicUsize::new(0));
                let denied_callback = denied.clone();
                let window = persistent_window(app.clone(), "auth-probe".into(), profile, initial, policy,
                    Arc::new(move || { denied_callback.fetch_add(1, std::sync::atomic::Ordering::SeqCst); }), false).await?;
                if mode == "clear" {
                    clear_browser_data(&window).await?;
                    window.navigate(url.clone()).map_err(|_| "PROBE_NAVIGATION_FAILED")?;
                }
                let storage = inspect(&window, &url).await?;
                if mode == "popup" {
                    for _ in 0..6 {
                        window.eval("window.probe.handoff=false; window.probe.childClosed=false; document.querySelector('#start').click()").map_err(|_| "PROBE_EVAL_FAILED")?;
                        wait_probe(&window, "handoff").await?;
                        wait_probe(&window, "childClosed").await?;
                        wait_children(&app, 0).await?;
                    }
                    window.eval("window.probe.keepOpen = true; document.querySelector('#start').click()").map_err(|_| "PROBE_EVAL_FAILED")?;
                    wait_probe(&window, "secondHandoff").await?;
                    wait_children(&app, 1).await?;
                    let child = app.webview_windows().into_values().find(|w| w.label().starts_with("auth-probe-popup-")).unwrap();
                    let child_url = child.url().map_err(|_| "PROBE_URL_FAILED")?;
                    child.navigate("http://127.0.0.1:1/blocked".parse().unwrap()).map_err(|_| "PROBE_NAVIGATION_FAILED")?;
                    tokio::time::sleep(Duration::from_millis(300)).await;
                    if child.url().map_err(|_| "PROBE_URL_FAILED")? != child_url {
                        return Err("PROBE_CHILD_NAVIGATION_ESCAPED_POLICY");
                    }
                    window.eval("window.open('http://127.0.0.1:1/blocked')").map_err(|_| "PROBE_EVAL_FAILED")?;
                    for i in 0..3 {
                        window.eval(format!("window.open('about:blank','quota-{i}')")).map_err(|_| "PROBE_EVAL_FAILED")?;
                        wait_children(&app, i + 2).await?;
                    }
                    window.eval("window.open('about:blank','quota-overflow')").map_err(|_| "PROBE_EVAL_FAILED")?;
                    for _ in 0..100 {
                        if denied.load(std::sync::atomic::Ordering::SeqCst) == 2 { break; }
                        tokio::time::sleep(Duration::from_millis(50)).await;
                    }
                    if denied.load(std::sync::atomic::Ordering::SeqCst) != 2 { return Err("PROBE_POPUP_ADMISSION_FAILED"); }
                    wait_children(&app, 4).await?;
                    window.destroy().map_err(|_| "PROBE_CLOSE_FAILED")?;
                    for _ in 0..100 {
                        if !app.webview_windows().into_values().any(|w| w.label().starts_with("auth-probe-popup-")) {
                            return Ok(json!({"ok":true,"mode":mode,"provider":provider,"opener_handoff":true,
                                "cross_origin_redirect":true,"shared_cookie_and_storage":true,"script_close":true,
                                "six_repeated_attempts":true,"navigation_denied":true,"four_child_limit":true,
                                "parent_close_cleans_children":true}));
                        }
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                    return Err("PROBE_ORPHAN_POPUP");
                }
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
    app.run(|_, event| {
        if let tauri::RunEvent::ExitRequested {
            code: None, api, ..
        } = event
        {
            // Parent-close acceptance must finish recording after the last
            // window is destroyed. Explicit app.exit(code) still terminates.
            api.prevent_exit();
        }
    });
}
