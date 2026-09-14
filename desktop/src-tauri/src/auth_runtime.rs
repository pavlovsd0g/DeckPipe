use crate::auth_broker::{provider_url, Broker, PublicStatus};
use serde_json::{json, Value};
use std::{
    future::Future,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

#[derive(Clone)]
pub struct AuthRuntime {
    broker: Arc<Mutex<Broker>>,
    transactions: Arc<Mutex<()>>,
    client: reqwest::Client,
    base_url: String,
    launch_token: String,
    broker_token: String,
    started: Instant,
    #[cfg(test)]
    preparing_commit: Arc<tokio::sync::Semaphore>,
}
impl AuthRuntime {
    pub fn new(
        base_url: String,
        launch_token: String,
        broker_token: String,
    ) -> Result<Self, &'static str> {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|_| "AUTH_BACKEND_UNAVAILABLE")?;
        Ok(Self {
            broker: Arc::new(Mutex::new(Broker::default())),
            transactions: Arc::new(Mutex::new(())),
            client,
            base_url,
            launch_token,
            broker_token,
            started: Instant::now(),
            #[cfg(test)]
            preparing_commit: Arc::new(tokio::sync::Semaphore::new(0)),
        })
    }
    fn now(&self) -> u64 {
        self.started.elapsed().as_secs()
    }
    fn discard_later(&self, round: Option<String>) {
        if let Some(round) = round {
            let runtime = self.clone();
            tokio::spawn(async move {
                let _ = runtime.backend("discard", json!({"requestId":round})).await;
            });
        }
    }
    pub async fn begin(&self, provider: &str) -> Result<PublicStatus, &'static str> {
        provider_url(provider)?;
        let mut broker = self.broker.lock().await;
        let old = broker.active_round();
        let status = broker.begin(provider, self.now())?;
        self.discard_later(old);
        Ok(status)
    }
    pub async fn status(&self, request: Option<&str>) -> Result<PublicStatus, &'static str> {
        let mut broker = self.broker.lock().await;
        let status = broker.status(request, self.now())?;
        if !status.pending() {
            self.discard_later(broker.take_round());
        }
        Ok(status)
    }
    pub async fn public_status(&self, request: Option<&str>) -> Result<Value, &'static str> {
        let mut status = serde_json::to_value(self.status(request).await?)
            .map_err(|_| "AUTH_INVALID_RESPONSE")?;
        status["authMode"] = json!("embedded");
        if request.is_none() {
            let accounts = self.backend("status", json!({})).await?;
            let mut safe = json!({});
            for provider in ["deezer", "sc"] {
                let source = &accounts["accounts"][provider];
                let account = &source["account"];
                safe[provider] = json!({"connected":source["connected"].as_bool().unwrap_or(false),"account":if account.is_object(){json!({"id":account["id"].as_str().unwrap_or(""),"name":account["name"].as_str().unwrap_or("")})}else{Value::Null}});
            }
            status["accounts"] = safe;
        }
        Ok(status)
    }
    pub async fn cancel(&self, request: &str) -> Result<PublicStatus, &'static str> {
        let mut broker = self.broker.lock().await;
        let status = broker.cancel(request, self.now())?;
        self.discard_later(broker.take_round());
        Ok(status)
    }
    pub async fn cancel_provider(&self, provider: &str) -> Result<(), &'static str> {
        provider_url(provider)?;
        let mut broker = self.broker.lock().await;
        let status = broker.status(None, self.now())?;
        if status.provider == provider && status.pending() {
            broker.cancel(&status.request_id, self.now())?;
            self.discard_later(broker.take_round());
        }
        Ok(())
    }
    pub async fn logout_with_clear<F>(
        &self,
        provider: &str,
        clear: F,
    ) -> Result<PublicStatus, &'static str>
    where
        F: Future<Output = Result<(), &'static str>>,
    {
        provider_url(provider)?;
        // Revoke before waiting for another provider's backend transaction.
        // Browser maintenance never owns the broker state or transaction lock.
        self.cancel_provider(provider).await?;
        let transaction = self.transactions.lock().await;
        let mut broker = self.broker.lock().await;
        if broker.status(None, self.now())?.provider == provider {
            self.discard_later(broker.take_round());
            broker.logout(provider);
        }
        drop(broker);
        let backend = self.backend("logout", json!({"provider":provider})).await;
        drop(transaction);
        let cleared = clear.await;
        cleared?;
        backend?;
        Ok(PublicStatus::idle(provider))
    }
    pub async fn notice(&self, request: &str, code: &'static str) {
        self.broker.lock().await.notice(request, code, self.now());
    }
    pub async fn fail(&self, request: &str, code: &'static str) {
        let mut broker = self.broker.lock().await;
        broker.fail(request, code, self.now());
        if broker
            .status(Some(request), self.now())
            .is_ok_and(|s| !s.pending())
        {
            self.discard_later(broker.take_round());
        }
    }
    async fn backend(&self, operation: &str, body: Value) -> Result<Value, &'static str> {
        let response = self
            .client
            .post(format!("{}/api/internal/auth/{}", self.base_url, operation))
            .bearer_auth(&self.launch_token)
            .header("X-DeckPipe-Auth-Broker", &self.broker_token)
            .timeout(Duration::from_secs(if operation == "complete" {
                120
            } else {
                10
            }))
            .json(&body)
            .send()
            .await
            .map_err(|_| "AUTH_BACKEND_UNAVAILABLE")?;
        if !response.status().is_success() {
            return Err("AUTH_PROVIDER_REJECTED");
        }
        let bytes = response
            .bytes()
            .await
            .map_err(|_| "AUTH_BACKEND_UNAVAILABLE")?;
        if bytes.len() > 16384 {
            return Err("AUTH_INVALID_RESPONSE");
        }
        serde_json::from_slice(&bytes).map_err(|_| "AUTH_INVALID_RESPONSE")
    }
    pub async fn complete(
        &self,
        window: &str,
        request: &str,
        provider: &str,
        credential: String,
    ) -> Result<PublicStatus, &'static str> {
        let round = self.broker.lock().await.validate(
            request,
            provider,
            window,
            &credential,
            self.now(),
        )?;
        // Each validation uses a new *private* backend request ID. AuthService
        // tombstones rejected/timed-out rounds permanently for their lifetime;
        // a different cookie can retry without reopening an older transaction.
        let prepared = self
            .backend(
                "complete",
                json!({"requestId":round,"provider":provider,"credential":credential}),
            )
            .await;
        let prepared = match prepared {
            Ok(value)
                if value["validationId"]
                    .as_str()
                    .is_some_and(|v| !v.is_empty()) =>
            {
                value
            }
            result => {
                let code = result.err().unwrap_or("AUTH_INVALID_RESPONSE");
                self.discard_later(Some(round.clone()));
                self.broker
                    .lock()
                    .await
                    .rejected(request, &round, code, self.now());
                return Err(code);
            }
        };
        #[cfg(test)]
        self.preparing_commit.add_permits(1);
        // Recheck the attempt only after admission to the backend transaction.
        // A cancel can revoke while an unrelated provider's logout is in flight.
        let _transaction = self.transactions.lock().await;
        let mut broker = self.broker.lock().await;
        if !broker.can_commit(request, &round, self.now()) {
            self.discard_later(Some(round));
            return Err("AUTH_CANCELLED");
        }
        // Commit contains no provider I/O. Holding the native lock orders its
        // final transaction before replacement/cancel/logout. Authorization time
        // is used if a successful response crosses the attempt deadline.
        let authorized_at = self.now();
        let committed = self
            .backend(
                "commit",
                json!({"requestId":round,"validationId":prepared["validationId"]}),
            )
            .await;
        match committed {
            Ok(value) if value["account"].is_object() => {
                broker.succeed(request, &round, value["account"].clone(), authorized_at)
            }
            result => {
                let code = result.err().unwrap_or("AUTH_INVALID_RESPONSE");
                // An uncertain commit must never be retried automatically.
                // Tombstone while still ordered against logout/replacement.
                let _ = self.backend("discard", json!({"requestId":round})).await;
                broker.fail(request, code, self.now());
                Err(code)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
        sync::Semaphore,
    };

    struct Fixture {
        entered: Semaphore,
        release: Semaphore,
        block_validation: bool,
        block_commit: AtomicBool,
        commit_entered: Semaphore,
        commit_release: Semaphore,
        fail_logout: AtomicBool,
        block_logout: AtomicBool,
        logout_entered: Semaphore,
        logout_release: Semaphore,
        completes: AtomicUsize,
        commits: AtomicUsize,
        operations: Mutex<Vec<(String, Value)>>,
    }
    async fn fixture(block_validation: bool) -> (AuthRuntime, Arc<Fixture>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let state = Arc::new(Fixture {
            entered: Semaphore::new(0),
            release: Semaphore::new(0),
            block_validation,
            block_commit: AtomicBool::new(false),
            commit_entered: Semaphore::new(0),
            commit_release: Semaphore::new(0),
            fail_logout: AtomicBool::new(false),
            block_logout: AtomicBool::new(false),
            logout_entered: Semaphore::new(0),
            logout_release: Semaphore::new(0),
            completes: AtomicUsize::new(0),
            commits: AtomicUsize::new(0),
            operations: Mutex::new(Vec::new()),
        });
        let server = state.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let server = server.clone();
                tokio::spawn(async move {
                    let mut bytes = Vec::new();
                    let (end, length) = loop {
                        let mut chunk = [0; 4096];
                        let n = stream.read(&mut chunk).await.unwrap();
                        if n == 0 {
                            return;
                        }
                        bytes.extend_from_slice(&chunk[..n]);
                        if let Some(pos) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
                            let header = String::from_utf8_lossy(&bytes[..pos]).to_lowercase();
                            assert!(header.contains("authorization: bearer test-launch"));
                            assert!(header.contains("x-deckpipe-auth-broker: test-broker"));
                            let length: usize = header
                                .lines()
                                .find_map(|l| l.strip_prefix("content-length:"))
                                .unwrap()
                                .trim()
                                .parse()
                                .unwrap();
                            break (pos + 4, length);
                        }
                    };
                    while bytes.len() < end + length {
                        let mut chunk = [0; 4096];
                        let n = stream.read(&mut chunk).await.unwrap();
                        if n == 0 {
                            return;
                        }
                        bytes.extend_from_slice(&chunk[..n]);
                    }
                    let header = String::from_utf8_lossy(&bytes[..end]);
                    let operation = header
                        .lines()
                        .next()
                        .unwrap()
                        .split_whitespace()
                        .nth(1)
                        .unwrap()
                        .rsplit('/')
                        .next()
                        .unwrap()
                        .to_owned();
                    let body: Value = serde_json::from_slice(&bytes[end..end + length]).unwrap();
                    server
                        .operations
                        .lock()
                        .await
                        .push((operation.clone(), body.clone()));
                    let mut code = "200 OK";
                    let result = match operation.as_str() {
                        "complete" => {
                            server.completes.fetch_add(1, Ordering::SeqCst);
                            server.entered.add_permits(1);
                            if body["credential"] == "invalid" {
                                code = "400 Bad Request";
                                json!({})
                            } else {
                                if server.block_validation {
                                    server.release.acquire().await.unwrap().forget();
                                }
                                json!({"validationId":"test-validation","account":{"id":"safe","name":"Safe"}})
                            }
                        }
                        "commit" => {
                            server.commits.fetch_add(1, Ordering::SeqCst);
                            server.commit_entered.add_permits(1);
                            if server.block_commit.load(Ordering::SeqCst) {
                                server.commit_release.acquire().await.unwrap().forget();
                            }
                            json!({"account":{"id":"safe","name":"Safe","credential":"synthetic-sentinel"}})
                        }
                        "logout" if server.fail_logout.load(Ordering::SeqCst) => {
                            code = "500 Internal Server Error";
                            json!({})
                        }
                        "logout" => {
                            server.logout_entered.add_permits(1);
                            if server.block_logout.load(Ordering::SeqCst) {
                                server.logout_release.acquire().await.unwrap().forget();
                            }
                            json!({"ok":true})
                        }
                        "status" => {
                            json!({"accounts":{"sc":{"connected":true,"account":{"id":"safe","name":"Safe","credential":"synthetic-sentinel"},"token":"synthetic-sentinel"}}})
                        }
                        _ => json!({"ok":true}),
                    };
                    let body = serde_json::to_vec(&result).unwrap();
                    let header = format!("HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len());
                    let _ = stream.write_all(header.as_bytes()).await;
                    let _ = stream.write_all(&body).await;
                });
            }
        });
        (
            AuthRuntime::new(
                format!("http://{address}"),
                "test-launch".into(),
                "test-broker".into(),
            )
            .unwrap(),
            state,
        )
    }
    async fn start_validation(
        runtime: &AuthRuntime,
    ) -> (
        String,
        tokio::task::JoinHandle<Result<PublicStatus, &'static str>>,
    ) {
        let status = runtime.begin("sc").await.unwrap();
        let request = status.request_id.clone();
        let auth = runtime.clone();
        let task = tokio::spawn(async move {
            auth.complete(
                &format!("auth-{request}"),
                &request,
                "sc",
                "synthetic-sentinel".into(),
            )
            .await
        });
        (status.request_id, task)
    }

    #[tokio::test]
    async fn cancel_returns_during_provider_io_and_no_late_commit() {
        let (runtime, server) = fixture(true).await;
        let (request, task) = start_validation(&runtime).await;
        server.entered.acquire().await.unwrap().forget();
        let result = tokio::time::timeout(Duration::from_millis(200), runtime.cancel(&request))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(result.status, "cancelled");
        server.release.add_permits(1);
        assert!(task.await.unwrap().is_err());
        assert_eq!(server.commits.load(Ordering::SeqCst), 0);
        assert!(!serde_json::to_string(&result)
            .unwrap()
            .contains("synthetic-sentinel"));
    }

    #[tokio::test]
    async fn replacement_and_logout_revoke_validation_even_when_logout_fails() {
        for action in ["replace", "logout", "logout-failed"] {
            let (runtime, server) = fixture(true).await;
            let (_, task) = start_validation(&runtime).await;
            server.entered.acquire().await.unwrap().forget();
            if action == "replace" {
                runtime.begin("deezer").await.unwrap();
            } else {
                server
                    .fail_logout
                    .store(action == "logout-failed", Ordering::SeqCst);
                let outcome = runtime.logout_with_clear("sc", async { Ok(()) }).await;
                assert_eq!(outcome.is_err(), action == "logout-failed");
            }
            server.release.add_permits(1);
            assert!(task.await.unwrap().is_err());
            assert_eq!(server.commits.load(Ordering::SeqCst), 0);
        }
    }

    #[tokio::test]
    async fn invalid_then_new_cookie_uses_unique_backend_round_and_never_retries_identical_material(
    ) {
        let (runtime, server) = fixture(false).await;
        let status = runtime.begin("sc").await.unwrap();
        let w = format!("auth-{}", status.request_id);
        assert!(runtime
            .complete(&w, &status.request_id, "sc", "invalid".into())
            .await
            .is_err());
        assert_eq!(
            runtime.status(None).await.unwrap().status,
            "waiting_browser"
        );
        for _ in 0..5 {
            assert!(matches!(
                runtime
                    .complete(&w, &status.request_id, "sc", "invalid".into())
                    .await,
                Err("AUTH_UNCHANGED_CREDENTIAL")
            ));
        }
        assert_eq!(server.completes.load(Ordering::SeqCst), 1);
        let connected = runtime
            .complete(&w, &status.request_id, "sc", "synthetic-sentinel".into())
            .await
            .unwrap();
        assert_eq!(connected.status, "connected");
        assert_eq!(server.commits.load(Ordering::SeqCst), 1);
        let operations = server.operations.lock().await;
        let rounds: Vec<_> = operations
            .iter()
            .filter(|(op, _)| op == "complete")
            .map(|(_, body)| body["requestId"].as_str().unwrap())
            .collect();
        assert_ne!(rounds[0], rounds[1]);
        assert!(!rounds.contains(&status.request_id.as_str()));
        assert!(!serde_json::to_string(&connected)
            .unwrap()
            .contains("synthetic-sentinel"));
    }

    #[tokio::test]
    async fn clear_failure_is_visible_and_logout_is_provider_selective() {
        let (runtime, server) = fixture(true).await;
        let (_, task) = start_validation(&runtime).await;
        server.entered.acquire().await.unwrap().forget();
        assert!(matches!(
            runtime
                .logout_with_clear("sc", async { Err("AUTH_BROWSER_CLEAR_FAILED") })
                .await,
            Err("AUTH_BROWSER_CLEAR_FAILED")
        ));
        server.release.add_permits(1);
        assert!(task.await.unwrap().is_err());
        assert_eq!(server.commits.load(Ordering::SeqCst), 0);
        let operations = server.operations.lock().await;
        let logouts: Vec<_> = operations.iter().filter(|(op, _)| op == "logout").collect();
        assert_eq!(logouts.len(), 1);
        assert_eq!(logouts[0].1["provider"], "sc");
        drop(operations);
        let status = runtime.public_status(None).await.unwrap();
        assert_eq!(status["authMode"], "embedded");
        assert!(status.get("helper").is_none());
        assert!(!status.to_string().contains("synthetic-sentinel"));
    }

    #[tokio::test]
    async fn browser_clear_is_attempted_even_if_backend_logout_fails() {
        let (runtime, server) = fixture(false).await;
        server.fail_logout.store(true, Ordering::SeqCst);
        let cleared = Arc::new(AtomicBool::new(false));
        let marker = cleared.clone();
        assert!(runtime
            .logout_with_clear("deezer", async move {
                marker.store(true, Ordering::SeqCst);
                Ok(())
            })
            .await
            .is_err());
        assert!(cleared.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn expiry_during_provider_io_discards_late_result() {
        let (mut runtime, server) = fixture(true).await;
        let (_, task) = start_validation(&runtime).await;
        server.entered.acquire().await.unwrap().forget();
        runtime.started -= Duration::from_secs(301);
        assert_eq!(runtime.status(None).await.unwrap().status, "expired");
        server.release.add_permits(1);
        assert!(task.await.unwrap().is_err());
        assert_eq!(server.commits.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn final_commit_is_ordered_before_logout_transaction() {
        let (runtime, server) = fixture(false).await;
        server.block_commit.store(true, Ordering::SeqCst);
        let (_, task) = start_validation(&runtime).await;
        server.commit_entered.acquire().await.unwrap().forget();
        let auth = runtime.clone();
        let mut logout =
            tokio::spawn(async move { auth.logout_with_clear("sc", async { Ok(()) }).await });
        assert!(tokio::time::timeout(Duration::from_millis(30), &mut logout)
            .await
            .is_err());
        assert!(!server
            .operations
            .lock()
            .await
            .iter()
            .any(|(op, _)| op == "logout"));
        server.commit_release.add_permits(1);
        assert_eq!(task.await.unwrap().unwrap().status, "connected");
        assert_eq!(logout.await.unwrap().unwrap().status, "idle");
        assert_eq!(runtime.status(None).await.unwrap().status, "idle");
        let operations = server.operations.lock().await;
        let commit = operations
            .iter()
            .position(|(op, _)| op == "commit")
            .unwrap();
        let logout = operations
            .iter()
            .position(|(op, _)| op == "logout")
            .unwrap();
        assert!(commit < logout);
    }

    async fn cancel_during_other_profile_cleanup(main_command: bool) {
        let (runtime, server) = fixture(true).await;
        let (request, validation) = start_validation(&runtime).await;
        server.entered.acquire().await.unwrap().forget();
        let browser = crate::auth_browser::AuthBrowser::new(runtime.clone()).unwrap();
        let clear_entered = Arc::new(Semaphore::new(0));
        let clear_release = Arc::new(Semaphore::new(0));
        let entered = clear_entered.clone();
        let release = clear_release.clone();
        let cleaning_browser = browser.clone();
        let cleanup = tokio::spawn(async move {
            // This is the same lifecycle coordinator called by the Tauri logout
            // command; only actual window destruction and native clear are fake.
            cleaning_browser
                .logout_with_cleanup("deezer", async { Ok(()) }, async move {
                    entered.add_permits(1);
                    release.acquire().await.unwrap().forget();
                    Ok(())
                })
                .await
        });
        clear_entered.acquire().await.unwrap().forget();
        let auth = runtime.clone();
        let closed = Arc::new(AtomicBool::new(false));
        let close_marker = closed.clone();
        let mut cancellation = tokio::spawn(async move {
            if main_command {
                // Same method and lock ordering as AuthBrowser::cancel, with
                // just its request-specific destroy side effect substituted.
                browser
                    .cancel_with_close(&request, || close_marker.store(true, Ordering::SeqCst))
                    .await
            } else {
                // Native CloseRequested/Destroyed callbacks call this path.
                auth.cancel(&request).await
            }
        });
        let prompt = tokio::time::timeout(Duration::from_millis(200), &mut cancellation)
            .await
            .ok();
        // Deliver prepare after cancellation was requested, while other-profile
        // clear is still held. Signal at native commit authorization makes the
        // old lifecycle/broker FIFO race deterministic rather than a sleep race.
        server.release.add_permits(1);
        runtime.preparing_commit.acquire().await.unwrap().forget();
        clear_release.add_permits(1);
        cleanup.await.unwrap().unwrap();
        let returned_promptly = prompt.is_some();
        let cancelled = match prompt {
            Some(result) => result.unwrap().unwrap(),
            None => cancellation.await.unwrap().unwrap(),
        };
        let result = validation.await.unwrap();
        assert_eq!(
            server.commits.load(Ordering::SeqCst),
            0,
            "cleanup must not admit a completion after cancellation was requested"
        );
        assert!(
            returned_promptly,
            "cancellation waited for unrelated profile clear"
        );
        assert_eq!(cancelled.status, "cancelled");
        assert!(result.is_err());
        assert_eq!(closed.load(Ordering::SeqCst), main_command);
    }

    #[tokio::test]
    async fn main_cancel_bypasses_delayed_other_provider_cleanup() {
        cancel_during_other_profile_cleanup(true).await;
    }

    #[tokio::test]
    async fn window_close_bypasses_delayed_other_provider_cleanup() {
        cancel_during_other_profile_cleanup(false).await;
    }

    #[tokio::test]
    async fn queued_completion_rechecks_cancel_after_other_backend_logout() {
        let (runtime, server) = fixture(true).await;
        let (request, validation) = start_validation(&runtime).await;
        server.entered.acquire().await.unwrap().forget();
        server.block_logout.store(true, Ordering::SeqCst);
        let browser = crate::auth_browser::AuthBrowser::new(runtime.clone()).unwrap();
        let cleaning = browser.clone();
        let cleanup = tokio::spawn(async move {
            cleaning
                .logout_with_cleanup("deezer", async { Ok(()) }, async { Ok(()) })
                .await
        });
        server.logout_entered.acquire().await.unwrap().forget();
        server.release.add_permits(1);
        // Completion is now queued behind backend logout, before cancellation.
        runtime.preparing_commit.acquire().await.unwrap().forget();
        let cancelled = tokio::time::timeout(
            Duration::from_millis(200),
            browser.cancel_with_close(&request, || {}),
        )
        .await;
        server.logout_release.add_permits(1);
        cleanup.await.unwrap().unwrap();
        assert_eq!(cancelled.unwrap().unwrap().status, "cancelled");
        assert!(validation.await.unwrap().is_err());
        assert_eq!(server.commits.load(Ordering::SeqCst), 0);
    }
}
