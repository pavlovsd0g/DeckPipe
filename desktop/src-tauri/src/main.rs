// DeckPipe: Tauri shell — sidecar-бэкенд + окно логина с подхватом cookie.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

#[tauri::command]
async fn service_login(app: tauri::AppHandle, service: String) -> Result<String, String> {
    let (url, cookie_name, title) = match service.as_str() {
        "deezer" => ("https://www.deezer.com/login", "arl", "Deezer"),
        "sc" => ("https://soundcloud.com/sign-in", "oauth_token", "SoundCloud"),
        _ => return Err("неизвестный сервис".into()),
    };
    let label = format!("login-{service}");
    let win = WebviewWindowBuilder::new(
        &app,
        label.clone(),
        WebviewUrl::External(url.parse().map_err(|e| format!("{e}"))?),
    )
    .title(format!("DeckPipe — вход {title}"))
    .inner_size(500.0, 780.0)
    .build()
    .map_err(|e| e.to_string())?;

    for _ in 0..300 {
        match app.get_webview_window(&label) {
            None => return Err("окно входа закрыто".into()),
            Some(w) => {
                if let Ok(cookies) = w.cookies() {
                    if let Some(c) = cookies.iter().find(|c| c.name() == cookie_name && !c.value().is_empty()) {
                        let value = c.value().to_string();
                        let _ = w.close();
                        return Ok(value);
                    }
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
    Err("таймаут ожидания входа (5 мин)".into())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![service_login])
        .setup(|app| {
            let shell = app.shell();
            let (mut rx, _child) = shell
                .sidecar("deckpipe-backend")
                .map_err(|e| e.to_string())?
                .args(["--port", "0"])
                .spawn()
                .map_err(|e| e.to_string())?;

            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut port: Option<u16> = None;
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stdout(line) = event {
                        let text = String::from_utf8_lossy(&line);
                        if let Some(rest) = text.strip_prefix("DECKPIPE_PORT=") {
                            port = rest.trim().parse().ok();
                            break;
                        }
                    }
                }
                let port = port.unwrap_or(7100);
                let url = format!("http://127.0.0.1:{port}");
                WebviewWindowBuilder::new(
                    &app_handle,
                    "main",
                    WebviewUrl::External(url.parse().expect("url")),
                )
                .title("DeckPipe")
                .inner_size(1320.0, 840.0)
                .min_inner_size(1000.0, 640.0)
                .build()
                .expect("main window");
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running DeckPipe");
}
