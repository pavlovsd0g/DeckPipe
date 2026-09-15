//! Persistent, provider-isolated WebView2 factory. Production entry URLs are
//! fixed by begin(); only the unshipped example supplies synthetic local URLs.
use crate::{
    auth_broker::{provider_url, PublicStatus},
    auth_runtime::AuthRuntime,
};
use std::{
    collections::HashSet,
    net::IpAddr,
    path::{Path, PathBuf},
    sync::{Arc, Mutex as StdMutex},
    time::Duration,
};
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tokio::sync::{oneshot, Mutex};
use webview2_com::{
    ClearBrowsingDataCompletedHandler,
    Microsoft::Web::WebView2::Win32::{ICoreWebView2Profile2, ICoreWebView2_13},
    WindowCloseRequestedEventHandler,
};
use windows::core::Interface;

pub fn profile_directory(local_app_data: &Path, provider: &str) -> Result<PathBuf, &'static str> {
    provider_url(provider)?;
    if !local_app_data.is_absolute() {
        return Err("AUTH_PROFILE_UNAVAILABLE");
    }
    Ok(local_app_data
        .join("DeckPipe")
        .join("AuthBrowser")
        .join(provider))
}
pub fn cookie_origin(provider: &str) -> Result<&'static str, &'static str> {
    match provider {
        "deezer" => Ok("https://www.deezer.com/"),
        "sc" => Ok("https://soundcloud.com/"),
        _ => Err("AUTH_INVALID_PROVIDER"),
    }
}
pub fn cookie_is_owned(provider: &str, name: &str, domain: &str) -> bool {
    let domain = domain.strip_prefix('.').unwrap_or(domain);
    match provider {
        "deezer" => name == "arl" && matches!(domain, "deezer.com" | "www.deezer.com"),
        "sc" => name == "oauth_token" && matches!(domain, "soundcloud.com" | "www.soundcloud.com"),
        _ => false,
    }
}
pub fn navigation_allowed(url: &tauri::Url) -> bool {
    if url.scheme() != "https" || !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    let Some(host) = url.host_str() else {
        return false;
    };
    let host = host.trim_matches(['[', ']']).trim_end_matches('.');
    if host.eq_ignore_ascii_case("localhost")
        || host.ends_with(".localhost")
        || host.ends_with(".local")
        || !host.contains('.')
    {
        return false;
    }
    // Fixed provider URLs can navigate to HTTPS identity providers; literal IPs
    // have no login use here and include loopback, LAN and mapped IPv6 forms.
    host.parse::<IpAddr>().is_err()
}

pub type NavigationPolicy = Arc<dyn Fn(&tauri::Url) -> bool + Send + Sync>;

#[derive(Default)]
struct PopupState {
    closed: bool,
    labels: HashSet<String>,
}
struct PopupGroup {
    root: String,
    state: StdMutex<PopupState>,
}
impl PopupGroup {
    fn reserve(&self) -> Option<String> {
        let mut state = self.state.lock().unwrap();
        if state.closed || state.labels.len() >= 4 {
            return None;
        }
        let label = format!("{}-popup-{}", self.root, crate::auth_broker::random_id());
        state.labels.insert(label.clone());
        Some(label)
    }
    fn release(&self, label: &str) {
        self.state.lock().unwrap().labels.remove(label);
    }
    fn active(&self) -> bool {
        !self.state.lock().unwrap().closed
    }
    fn close(&self) -> Vec<String> {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        state.labels.iter().cloned().collect()
    }
}

fn popup_response(
    app: tauri::AppHandle,
    profile: PathBuf,
    policy: NavigationPolicy,
    blocked: Arc<dyn Fn() + Send + Sync>,
    group: Arc<PopupGroup>,
    url: tauri::Url,
    features: tauri::webview::NewWindowFeatures,
) -> tauri::webview::NewWindowResponse<tauri::Wry> {
    use tauri::webview::NewWindowResponse;
    // OAuth pages commonly open a blank child and assign its HTTPS URL later.
    // Every subsequent navigation still uses the same production URL policy.
    if url.as_str() != "about:blank" && !policy(&url) {
        blocked();
        return NewWindowResponse::Deny;
    }
    let Some(parent) = app.get_webview_window(&group.root) else {
        return NewWindowResponse::Deny;
    };
    let Some(label) = group.reserve() else {
        blocked();
        return NewWindowResponse::Deny;
    };
    let navigate_policy = policy.clone();
    let navigate_group = group.clone();
    let child_app = app.clone();
    let child_profile = profile.clone();
    let child_blocked = blocked.clone();
    let child_group = group.clone();
    // Wry 0.55 defers this callback past WebView2's NewWindowRequested event.
    // window_features supplies the opener's exact environment; returning Create
    // lets WebView2 retain window.opener/postMessage and perform the navigation.
    let builder = WebviewWindowBuilder::new(
        &app,
        &label,
        WebviewUrl::External("about:blank".parse().unwrap()),
    )
    .window_features(features)
    .title("DeckPipe — дополнительное окно входа")
    .inner_size(640.0, 760.0)
    .min_inner_size(480.0, 480.0)
    .data_directory(profile)
    .incognito(false)
    .devtools(false)
    .on_navigation(move |url| {
        navigate_group.active() && (url.as_str() == "about:blank" || navigate_policy(url))
    })
    .on_new_window(move |url, features| {
        popup_response(
            child_app.clone(),
            child_profile.clone(),
            policy.clone(),
            child_blocked.clone(),
            child_group.clone(),
            url,
            features,
        )
    });
    let window = builder.owner(&parent).and_then(|builder| builder.build());
    match window {
        Ok(window) => {
            let close_group = group.clone();
            let close_label = label.clone();
            window.on_window_event(move |event| {
                if matches!(event, tauri::WindowEvent::Destroyed) {
                    close_group.release(&close_label);
                }
            });
            // Wry handles window.close() by destroying its child HWND. Tauri's
            // outer window and registry must be destroyed as well, otherwise a
            // blank window and occupied popup slot survive each OAuth handoff.
            let close_window = window.clone();
            let close_failed = blocked.clone();
            if window
                .with_webview(move |webview| {
                    let requested = close_window.clone();
                    let result = (|| -> windows::core::Result<()> {
                        unsafe {
                            let core = webview.controller().CoreWebView2()?;
                            core.add_WindowCloseRequested(
                                &WindowCloseRequestedEventHandler::create(Box::new(move |_, _| {
                                    let window = requested.clone();
                                    tauri::async_runtime::spawn_blocking(move || {
                                        let _ = window.destroy();
                                    });
                                    Ok(())
                                })),
                                &mut 0,
                            )
                        }
                    })();
                    if result.is_err() {
                        close_failed();
                        tauri::async_runtime::spawn_blocking(move || {
                            let _ = close_window.destroy();
                        });
                    }
                })
                .is_err()
            {
                let _ = window.destroy();
                group.release(&label);
                blocked();
                return NewWindowResponse::Deny;
            }
            if !group.active() || app.get_webview_window(&group.root).is_none() {
                let _ = window.destroy();
                group.release(&label);
                return NewWindowResponse::Deny;
            }
            NewWindowResponse::Create { window }
        }
        Err(_) => {
            group.release(&label);
            blocked();
            NewWindowResponse::Deny
        }
    }
}

/// Called from async commands/tasks, never synchronously from a UI event.
/// All auth and maintenance/probe windows use these exact profile settings.
pub async fn persistent_window(
    app: tauri::AppHandle,
    label: String,
    profile: PathBuf,
    url: tauri::Url,
    policy: NavigationPolicy,
    popup_blocked: Arc<dyn Fn() + Send + Sync>,
    visible: bool,
) -> Result<WebviewWindow, &'static str> {
    tokio::task::spawn_blocking(move || {
        std::fs::create_dir_all(&profile).map_err(|_| "AUTH_PROFILE_UNAVAILABLE")?;
        let group = Arc::new(PopupGroup {
            root: label.clone(),
            state: StdMutex::default(),
        });
        let popup_app = app.clone();
        let popup_profile = profile.clone();
        let popup_policy = policy.clone();
        let popup_group = group.clone();
        let window = WebviewWindowBuilder::new(&app, label, WebviewUrl::External(url))
            .title("DeckPipe — вход в сервис")
            .inner_size(1000.0, 760.0)
            .min_inner_size(640.0, 480.0)
            .data_directory(profile)
            .incognito(false)
            .visible(visible)
            .devtools(false)
            .on_navigation(move |url| policy(url))
            .on_new_window(move |url, features| {
                popup_response(
                    popup_app.clone(),
                    popup_profile.clone(),
                    popup_policy.clone(),
                    popup_blocked.clone(),
                    popup_group.clone(),
                    url,
                    features,
                )
            })
            .build()
            .map_err(|_| "AUTH_BROWSER_UNAVAILABLE")?;
        window.on_window_event(move |event| {
            if matches!(
                event,
                tauri::WindowEvent::Destroyed | tauri::WindowEvent::CloseRequested { .. }
            ) {
                let labels = group.close();
                let app = app.clone();
                // Also covers cancellation/expiry/commit and a child still being
                // constructed when the parent closes. Other attempts are untouched.
                tauri::async_runtime::spawn_blocking(move || {
                    for label in labels {
                        if let Some(child) = app.get_webview_window(&label) {
                            let _ = child.destroy();
                        }
                    }
                });
            }
        });
        Ok(window)
    })
    .await
    .map_err(|_| "AUTH_BROWSER_UNAVAILABLE")?
}

/// Wry's clear_all_browsing_data ignores the completion HRESULT. Await it
/// ourselves so an asynchronous failure cannot be reported as successful exit.
pub async fn clear_browser_data(window: &WebviewWindow) -> Result<(), &'static str> {
    let (tx, rx) = oneshot::channel();
    let tx = Arc::new(std::sync::Mutex::new(Some(tx)));
    let completion = tx.clone();
    window
        .with_webview(move |webview| {
            let result = (|| -> windows::core::Result<()> {
                unsafe {
                    let profile = webview
                        .controller()
                        .CoreWebView2()?
                        .cast::<ICoreWebView2_13>()?
                        .Profile()?
                        .cast::<ICoreWebView2Profile2>()?;
                    profile.ClearBrowsingDataAll(&ClearBrowsingDataCompletedHandler::create(
                        Box::new(move |error| {
                            if let Some(tx) = completion.lock().unwrap().take() {
                                let _ = tx.send(error.map_err(|_| "AUTH_BROWSER_CLEAR_FAILED"));
                            }
                            Ok(())
                        }),
                    ))
                }
            })();
            if result.is_err() {
                if let Some(tx) = tx.lock().unwrap().take() {
                    let _ = tx.send(Err("AUTH_BROWSER_CLEAR_FAILED"));
                }
            }
        })
        .map_err(|_| "AUTH_BROWSER_CLEAR_FAILED")?;
    tokio::time::timeout(Duration::from_secs(30), rx)
        .await
        .map_err(|_| "AUTH_BROWSER_CLEAR_FAILED")?
        .map_err(|_| "AUTH_BROWSER_CLEAR_FAILED")?
}

#[derive(Clone)]
pub struct AuthBrowser {
    auth: AuthRuntime,
    local_app_data: PathBuf,
    lifecycle: Arc<Mutex<()>>,
}
impl AuthBrowser {
    pub fn new(auth: AuthRuntime) -> Result<Self, &'static str> {
        let local_app_data = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .ok_or("AUTH_PROFILE_UNAVAILABLE")?;
        profile_directory(&local_app_data, "deezer")?;
        Ok(Self {
            auth,
            local_app_data,
            lifecycle: Arc::new(Mutex::new(())),
        })
    }
    async fn destroy_attempt_windows(app: &tauri::AppHandle) -> Result<(), &'static str> {
        let mut windows: Vec<_> = app
            .webview_windows()
            .into_values()
            .filter(|w| w.label().starts_with("auth-"))
            .collect();
        // Close children before their Win32 owner; destroying the owner first
        // already destroys them and would make a second destroy report failure.
        windows.sort_by_key(|w| !w.label().contains("-popup-"));
        tokio::task::spawn_blocking(move || {
            for window in windows {
                window.destroy().map_err(|_| "AUTH_BROWSER_CLEAR_FAILED")?;
            }
            Ok(())
        })
        .await
        .map_err(|_| "AUTH_BROWSER_CLEAR_FAILED")?
    }
    pub async fn begin(
        &self,
        app: tauri::AppHandle,
        provider: &str,
    ) -> Result<PublicStatus, &'static str> {
        let url = provider_url(provider)?
            .parse()
            .map_err(|_| "AUTH_INVALID_PROVIDER")?;
        let profile = profile_directory(&self.local_app_data, provider)?;
        let _operation = self.lifecycle.lock().await;
        let old = self.auth.status(None).await?;
        if old.pending() {
            self.auth.cancel(&old.request_id).await?;
        }
        Self::destroy_attempt_windows(&app).await?;
        let status = self.auth.begin(provider).await?;
        let request = status.request_id.clone();
        let notify_auth = self.auth.clone();
        let notice_request = request.clone();
        let window = persistent_window(
            app.clone(),
            format!("auth-{request}"),
            profile,
            url,
            Arc::new(navigation_allowed),
            Arc::new(move || {
                let auth = notify_auth.clone();
                let request = notice_request.clone();
                tauri::async_runtime::spawn(async move {
                    auth.notice(&request, "AUTH_POPUP_BLOCKED").await;
                });
            }),
            true,
        )
        .await;
        let window = match window {
            Ok(window) => window,
            Err(code) => {
                self.auth.fail(&request, code).await;
                return Err(code);
            }
        };
        let auth = self.auth.clone();
        let close_request = request.clone();
        window.on_window_event(move |event| {
            if matches!(
                event,
                tauri::WindowEvent::Destroyed | tauri::WindowEvent::CloseRequested { .. }
            ) {
                let auth = auth.clone();
                let request = close_request.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = auth.cancel(&request).await;
                });
            }
        });
        self.poll(app, window, status.clone());
        Ok(status)
    }
    fn poll(&self, app: tauri::AppHandle, window: WebviewWindow, attempt: PublicStatus) {
        let auth = self.auth.clone();
        tauri::async_runtime::spawn(async move {
            loop {
                let status = auth.status(Some(&attempt.request_id)).await;
                match status {
                    Ok(status) if status.pending() => {
                        if status.status == "waiting_browser" {
                            let source = window.clone();
                            let provider = attempt.provider.clone();
                            let credential = tokio::task::spawn_blocking(move || {
                                let origin = cookie_origin(&provider)?
                                    .parse()
                                    .map_err(|_| "AUTH_INVALID_PROVIDER")?;
                                let cookies = source
                                    .cookies_for_url(origin)
                                    .map_err(|_| "AUTH_BROWSER_UNAVAILABLE")?;
                                Ok::<_, &'static str>(
                                    cookies
                                        .into_iter()
                                        .find(|c| {
                                            cookie_is_owned(
                                                &provider,
                                                c.name(),
                                                c.domain().unwrap_or(""),
                                            )
                                        })
                                        .map(|c| c.value().to_owned()),
                                )
                            })
                            .await;
                            match credential {
                                Ok(Ok(Some(credential))) => {
                                    let auth = auth.clone();
                                    let request = attempt.request_id.clone();
                                    let provider = attempt.provider.clone();
                                    let label = window.label().to_owned();
                                    tauri::async_runtime::spawn(async move {
                                        let _ = auth
                                            .complete(&label, &request, &provider, credential)
                                            .await;
                                    });
                                }
                                Ok(Ok(None)) => {}
                                _ => {
                                    auth.fail(&attempt.request_id, "AUTH_BROWSER_UNAVAILABLE")
                                        .await;
                                }
                            }
                        }
                    }
                    result => {
                        let connected = result.is_ok_and(|s| s.status == "connected");
                        let _ = window.destroy();
                        if connected {
                            if let Some(main) = app.get_webview_window("main") {
                                let _ = main.unminimize();
                                let _ = main.show();
                                let _ = main.set_focus();
                            }
                        }
                        break;
                    }
                }
                tokio::time::sleep(Duration::from_millis(750)).await;
            }
        });
    }
    pub async fn cancel(
        &self,
        app: tauri::AppHandle,
        request: &str,
    ) -> Result<PublicStatus, &'static str> {
        self.cancel_with_close(request, || {
            if let Some(window) = app.get_webview_window(&format!("auth-{request}")) {
                let _ = window.destroy();
            }
        })
        .await
    }
    pub(crate) async fn cancel_with_close(
        &self,
        request: &str,
        close: impl FnOnce(),
    ) -> Result<PublicStatus, &'static str> {
        // Request-specific close cannot interfere with a replacement window;
        // cancellation must not wait for another profile's maintenance lock.
        let status = self.auth.cancel(request).await?;
        close();
        Ok(status)
    }
    pub async fn logout(
        &self,
        app: tauri::AppHandle,
        provider: &str,
    ) -> Result<PublicStatus, &'static str> {
        let profile = profile_directory(&self.local_app_data, provider)?;
        self.logout_with_cleanup(provider, Self::destroy_attempt_windows(&app), async {
            let window = persistent_window(
                app.clone(),
                format!("auth-clear-{}", crate::auth_broker::random_id()),
                profile,
                "about:blank".parse().unwrap(),
                Arc::new(|u| u.as_str() == "about:blank"),
                Arc::new(|| {}),
                false,
            )
            .await
            .map_err(|_| "AUTH_BROWSER_CLEAR_FAILED")?;
            let cleared = clear_browser_data(&window).await;
            let closed = window.destroy().map_err(|_| "AUTH_BROWSER_CLEAR_FAILED");
            cleared.and(closed)
        })
        .await
    }
    pub(crate) async fn logout_with_cleanup(
        &self,
        provider: &str,
        stop: impl std::future::Future<Output = Result<(), &'static str>>,
        clear: impl std::future::Future<Output = Result<(), &'static str>>,
    ) -> Result<PublicStatus, &'static str> {
        self.auth.cancel_provider(provider).await?;
        let _operation = self.lifecycle.lock().await;
        let status = self.auth.status(None).await?;
        // Only the selected service's window is stopped; the other service and
        // all library data stay independent of this profile operation.
        if status.provider == provider {
            if status.pending() {
                self.auth.cancel(&status.request_id).await?;
            }
            stop.await?;
        }
        self.auth.logout_with_clear(provider, clear).await
    }
}
