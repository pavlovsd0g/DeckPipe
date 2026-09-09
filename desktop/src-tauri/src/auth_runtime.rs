use crate::{
    auth_broker::{provider_url, random_id, Broker, PublicStatus},
    browser_bridge::{parse_request, read_bytes, write_value, NativeRequest},
    native_ipc,
};
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tokio::sync::{Mutex, Semaphore};

#[derive(Clone)]
pub struct AuthRuntime {
    broker: Arc<Mutex<Broker>>,
    client: reqwest::Client,
    base_url: String,
    launch_token: String,
    broker_token: String,
    started: Instant,
    bridge_failed: Arc<AtomicBool>,
    helper_connections: Arc<AtomicUsize>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[tokio::test]
    async fn failed_logout_revokes_claim_before_late_callback() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut input = [0; 4096];
            stream.read(&mut input).unwrap();
            stream.write_all(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}").unwrap();
        });
        let runtime = AuthRuntime::new(
            format!("http://{address}"),
            "test-launch".into(),
            "test-broker".into(),
        )
        .unwrap();
        runtime.begin("sc").await.unwrap();
        let claim = runtime.broker.lock().await.claim("test-helper", 0).unwrap();
        assert!(runtime.logout("sc").await.is_err());
        assert!(runtime
            .complete(
                "test-helper",
                &claim.request_id,
                "sc",
                &claim.state,
                "synthetic-secret".into()
            )
            .await
            .is_err());
        assert!(!runtime.broker.lock().await.can_commit(&claim.request_id, 0));
        assert_eq!(runtime.status(None).await.unwrap().status, "idle");
        server.join().unwrap();
    }
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
            client,
            base_url,
            launch_token,
            broker_token,
            started: Instant::now(),
            bridge_failed: Arc::new(AtomicBool::new(false)),
            helper_connections: Arc::new(AtomicUsize::new(0)),
        })
    }
    fn now(&self) -> u64 {
        self.started.elapsed().as_secs()
    }
    pub async fn begin(&self, provider: &str) -> Result<PublicStatus, &'static str> {
        self.broker.lock().await.begin(provider, self.now())
    }
    pub async fn status(&self, request: Option<&str>) -> Result<PublicStatus, &'static str> {
        self.broker.lock().await.status(request, self.now())
    }
    pub async fn public_status(&self, request: Option<&str>) -> Result<Value, &'static str> {
        let mut status = serde_json::to_value(self.status(request).await?)
            .map_err(|_| "AUTH_INVALID_RESPONSE")?;
        let failed = self.bridge_failed.load(Ordering::Relaxed);
        let connected = self.helper_connections.load(Ordering::Relaxed) > 0;
        status["helper"] = json!({"browser":"firefox","state":if failed{"unavailable"}else if connected{"connected"}else{"not_connected"},"setupRequired":!connected,"setupAvailable":true});
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
    pub fn bridge_failed(&self) {
        self.bridge_failed.store(true, Ordering::Relaxed);
    }
    pub async fn cancel(&self, request: &str) -> Result<PublicStatus, &'static str> {
        let status = self.broker.lock().await.cancel(request, self.now())?;
        let _ = self.backend("discard", json!({"requestId":request})).await;
        Ok(status)
    }
    pub async fn logout(&self, provider: &str) -> Result<PublicStatus, &'static str> {
        provider_url(provider)?;
        let mut broker = self.broker.lock().await;
        broker.logout(provider);
        self.backend("logout", json!({"provider":provider})).await?;
        Ok(PublicStatus {
            request_id: String::new(),
            provider: provider.into(),
            status: "idle".into(),
            expires_in: 0,
            account: None,
            error_code: None,
        })
    }
    pub async fn fail(&self, request: &str, code: &'static str) {
        self.broker.lock().await.fail(request, code, self.now());
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
    async fn complete(
        &self,
        connection: &str,
        request: &str,
        provider: &str,
        state: &str,
        credential: String,
    ) -> Result<Value, &'static str> {
        self.broker
            .lock()
            .await
            .validate(request, provider, state, connection, self.now())?;
        let prepared = self
            .backend(
                "complete",
                json!({"requestId":request,"provider":provider,"credential":credential}),
            )
            .await;
        let prepared = match prepared {
            Ok(value) => value,
            Err(code) => {
                self.fail(request, code).await;
                // A timed-out HTTP request can still finish provider validation
                // in Python. Tombstone it there before returning to the helper.
                let _ = self.backend("discard", json!({"requestId":request})).await;
                return Err(code);
            }
        };
        let validation = prepared
            .get("validationId")
            .and_then(Value::as_str)
            .ok_or("AUTH_INVALID_RESPONSE")?;
        let mut broker = self.broker.lock().await;
        if !broker.can_commit(request, self.now()) {
            drop(broker);
            let _ = self.backend("discard", json!({"requestId":request})).await;
            return Err("AUTH_CANCELLED");
        }
        // Commit does no provider I/O. Hold the attempt lock across the bounded
        // store transaction so begin/cancel/logout cannot race its final write.
        let commit_time = self.now();
        let committed = self
            .backend(
                "commit",
                json!({"requestId":request,"validationId":validation}),
            )
            .await;
        let committed = match committed {
            Ok(value) => value,
            Err(code) => {
                broker.fail(request, code, self.now());
                return Err(code);
            }
        };
        let account = committed
            .get("account")
            .cloned()
            .ok_or("AUTH_INVALID_RESPONSE")?;
        // A commit accepted before the deadline is final even if its response
        // crosses the deadline: use the timestamp at authorization of commit.
        let status = broker.succeed(request, account, commit_time)?;
        Ok(json!({"ok":true,"status":status}))
    }
    async fn handle(
        &self,
        connection: &str,
        request: NativeRequest,
    ) -> Result<Value, &'static str> {
        match request {
            NativeRequest::Hello { .. } => {
                let status = self.status(None).await?;
                if !matches!(status.status.as_str(), "waiting_browser" | "waiting_helper") {
                    return Err("NO_PENDING_LOGIN");
                }
                Ok(json!({"ok":true,"status":status}))
            }
            NativeRequest::ClaimPending { .. } => Ok(
                json!({"ok":true,"claim":self.broker.lock().await.claim(connection,self.now())?}),
            ),
            NativeRequest::Complete {
                request_id,
                provider,
                state,
                credential,
                ..
            } => {
                self.complete(connection, &request_id, &provider, &state, credential)
                    .await
            }
        }
    }
    pub async fn serve(
        self,
        on_connected: Arc<dyn Fn() + Send + Sync>,
    ) -> Result<(), &'static str> {
        let name = native_ipc::pipe_name()?;
        let expected = std::env::current_exe()
            .map_err(|_| "AUTH_HOST_UNAVAILABLE")?
            .with_file_name("deckpipe-auth-host.exe");
        let mut listener = native_ipc::create_server(&name, true)?;
        let capacity = Arc::new(Semaphore::new(4));
        loop {
            listener
                .connect()
                .await
                .map_err(|_| "AUTH_PIPE_UNAVAILABLE")?;
            let pipe = listener;
            listener = native_ipc::create_server(&name, false)?;
            let runtime = self.clone();
            let expected = expected.clone();
            let notify = on_connected.clone();
            let Ok(permit) = capacity.clone().try_acquire_owned() else {
                continue;
            };
            tokio::spawn(async move {
                let _permit = permit;
                runtime.connection(pipe, expected, notify).await;
            });
        }
    }
    async fn connection(
        &self,
        mut pipe: tokio::net::windows::named_pipe::NamedPipeServer,
        expected: PathBuf,
        notify: Arc<dyn Fn() + Send + Sync>,
    ) {
        if native_ipc::verify_client(&pipe, &expected).is_err() {
            return;
        }
        self.helper_connections.fetch_add(1, Ordering::Relaxed);
        let connection = random_id();
        loop {
            let bytes =
                match tokio::time::timeout(Duration::from_secs(305), read_bytes(&mut pipe)).await {
                    Ok(Ok(b)) => b,
                    _ => break,
                };
            let request = match parse_request(&bytes) {
                Ok(r) => r,
                Err(_) => break,
            };
            let response = self.handle(&connection, request).await;
            let connected = response.as_ref().is_ok_and(|r| {
                r.get("status")
                    .and_then(|s| s.get("status"))
                    .and_then(Value::as_str)
                    == Some("connected")
            });
            let value = match response {
                Ok(v) => v,
                Err(code) => json!({"ok":false,"errorCode":code}),
            };
            if write_value(&mut pipe, &value).await.is_err() {
                break;
            }
            if connected {
                notify();
                break;
            }
        }
        self.broker.lock().await.disconnected(&connection);
        self.helper_connections.fetch_sub(1, Ordering::Relaxed);
    }
}
