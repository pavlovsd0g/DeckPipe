#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use rand::{rngs::OsRng, RngCore};
use serde::Serialize;
use std::{
    env,
    sync::Mutex,
    time::Duration,
};
use tauri::{
    async_runtime::{block_on, Receiver},
    Manager, RunEvent, State, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::time::timeout;

const STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const LOGIN_CANCELLED: &str = "DECKPIPE_LOGIN_CANCELLED";
const CHILD_ENV_ALLOWLIST: &[&str] = &[
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATH",
];

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendConnection {
    base_url: String,
    token: String,
}

#[derive(Default)]
struct BackendConnectionState {
    connection: Mutex<Option<BackendConnection>>,
}

#[derive(Default)]
struct ManagedSidecar {
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
async fn backend_connection(
    window: WebviewWindow,
    connection: State<'_, BackendConnectionState>,
) -> Result<BackendConnection, String> {
    if window.label() != "main" {
        return Err("backend connection unavailable".into());
    }
    connection
        .connection
        .lock()
        .map_err(|_| "backend connection unavailable".to_string())?
        .take()
        .ok_or_else(|| "backend connection unavailable".to_string())
}

#[tauri::command]
async fn service_login(app: tauri::AppHandle, service: String) -> Result<String, String> {
    let (url, cookie_name, title) = match service.as_str() {
        "deezer" => ("https://www.deezer.com/login", "arl", "Deezer"),
        "sc" => ("https://soundcloud.com/sign-in", "oauth_token", "SoundCloud"),
        _ => return Err("unknown service".into()),
    };
    let label = format!("login-{service}");
    let win = WebviewWindowBuilder::new(
        &app,
        label.clone(),
        WebviewUrl::External(url.parse().map_err(|e| format!("{e}"))?),
    )
    .title(format!("DeckPipe login: {title}"))
    .inner_size(500.0, 780.0)
    .build()
    .map_err(|_| "login window unavailable".to_string())?;

    for _ in 0..300 {
        match app.get_webview_window(&label) {
            None => return Err(LOGIN_CANCELLED.into()),
            Some(w) => {
                if let Ok(cookies) = w.cookies() {
                    if let Some(c) = cookies
                        .iter()
                        .find(|c| c.name() == cookie_name && !c.value().is_empty())
                    {
                        let value = c.value().to_string();
                        let _ = w.close();
                        return Ok(value);
                    }
                }
            }
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    let _ = win.close();
    Err("login timed out".into())
}

fn retain_sidecar_child(state: &ManagedSidecar, child: CommandChild) -> Result<(), String> {
    let mut guard = match state.child.lock() {
        Ok(guard) => guard,
        Err(_) => {
            let _ = child.kill();
            return Err("backend lifecycle unavailable".to_string());
        }
    };
    if guard.is_some() {
        let _ = child.kill();
        return Err("backend already running".into());
    }
    *guard = Some(child);
    Ok(())
}

fn kill_sidecar(state: &ManagedSidecar) {
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.take() {
            let _ = child.kill();
        }
    }
}

fn store_backend_connection(
    state: &BackendConnectionState,
    connection: BackendConnection,
) -> Result<(), String> {
    let mut guard = state
        .connection
        .lock()
        .map_err(|_| "backend connection unavailable".to_string())?;
    if guard.is_some() {
        return Err("backend connection already initialized".into());
    }
    *guard = Some(connection);
    Ok(())
}

fn parse_port_line(text: &str) -> Result<Option<u16>, String> {
    let Some(rest) = text.strip_prefix("DECKPIPE_PORT=") else {
        return Ok(None);
    };
    let value = rest.trim();
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("backend startup failed".into());
    }
    let port: u16 = value.parse().map_err(|_| "backend startup failed".to_string())?;
    if port == 0 {
        return Err("backend startup failed".into());
    }
    Ok(Some(port))
}

async fn wait_for_backend_port(rx: &mut Receiver<CommandEvent>) -> Result<u16, String> {
    timeout(STARTUP_TIMEOUT, async {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = std::str::from_utf8(&line)
                        .map_err(|_| "backend startup failed".to_string())?;
                    if let Some(port) = parse_port_line(text)? {
                        return Ok(port);
                    }
                }
                CommandEvent::Terminated(_) | CommandEvent::Error(_) => {
                    return Err("backend startup failed".into());
                }
                CommandEvent::Stderr(_) => {}
                _ => {}
            }
        }
        Err("backend startup failed".into())
    })
    .await
    .map_err(|_| "backend startup timed out".to_string())?
}

fn generate_launch_token() -> String {
    let mut token_bytes = [0_u8; 32];
    OsRng.fill_bytes(&mut token_bytes);
    hex::encode(token_bytes)
}

fn sidecar_environment(token: &str) -> Vec<(String, String)> {
    let mut pairs: Vec<(String, String)> = CHILD_ENV_ALLOWLIST
        .iter()
        .filter_map(|name| env::var(name).ok().map(|value| ((*name).to_string(), value)))
        .collect();
    pairs.push(("DECKPIPE_API_TOKEN".into(), token.to_string()));
    pairs.push(("DECKPIPE_PARENT_PID".into(), std::process::id().to_string()));
    pairs
}

fn start_backend(app: &tauri::App) -> Result<BackendConnection, String> {
    let token = generate_launch_token();
    let sidecar = app.state::<ManagedSidecar>();
    let shell = app.shell();
    let (mut rx, child) = shell
        .sidecar("deckpipe-backend")
        .map_err(|_| "backend startup failed".to_string())?
        .env_clear()
        .envs(sidecar_environment(&token))
        .args(["--host", "127.0.0.1", "--port", "0"])
        .spawn()
        .map_err(|_| "backend startup failed".to_string())?;
    if let Err(error) = retain_sidecar_child(&sidecar, child) {
        return Err(error);
    }

    let port = match block_on(wait_for_backend_port(&mut rx)) {
        Ok(port) => port,
        Err(error) => {
            kill_sidecar(&sidecar);
            return Err(error);
        }
    };

    let app_handle = app.handle().clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            if matches!(event, CommandEvent::Terminated(_) | CommandEvent::Error(_)) {
                if let Some(sidecar) = app_handle.try_state::<ManagedSidecar>() {
                    kill_sidecar(&sidecar);
                }
                app_handle.exit(1);
                break;
            }
        }
    });

    Ok(BackendConnection {
        base_url: format!("http://127.0.0.1:{port}"),
        token,
    })
}

fn build_main_window(app: &tauri::App) -> Result<(), String> {
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("DeckPipe")
        .inner_size(1320.0, 840.0)
        .min_inner_size(1000.0, 640.0)
        .build()
        .map(|_| ())
        .map_err(|_| "main window unavailable".to_string())
}

fn main() {
    let app = tauri::Builder::default()
        .manage(BackendConnectionState::default())
        .manage(ManagedSidecar::default())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            service_login,
            backend_connection
        ])
        .setup(|app| {
            let connection = start_backend(app)?;
            let connection_state = app.state::<BackendConnectionState>();
            if let Err(error) = store_backend_connection(&connection_state, connection) {
                let sidecar = app.state::<ManagedSidecar>();
                kill_sidecar(&sidecar);
                return Err(error.into());
            }
            if let Err(error) = build_main_window(app) {
                let sidecar = app.state::<ManagedSidecar>();
                kill_sidecar(&sidecar);
                return Err(error.into());
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building DeckPipe");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            if let Some(sidecar) = app_handle.try_state::<ManagedSidecar>() {
                kill_sidecar(&sidecar);
            }
        }
    });
}
