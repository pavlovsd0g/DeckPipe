use rand::{rngs::OsRng, RngCore};
use serde::Serialize;
use serde_json::Value;

pub const ATTEMPT_SECONDS: u64 = 300;
pub fn random_id() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);
    hex::encode(bytes)
}

pub fn provider_url(provider: &str) -> Result<&'static str, &'static str> {
    match provider {
        "deezer" => Ok("https://www.deezer.com/login"),
        "sc" => Ok("https://soundcloud.com/sign-in"),
        _ => Err("AUTH_INVALID_PROVIDER"),
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PublicStatus {
    pub request_id: String,
    pub provider: String,
    pub status: String,
    pub expires_in: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub account: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Claim {
    pub request_id: String,
    pub provider: String,
    pub state: String,
}

struct Attempt {
    public: PublicStatus,
    state: String,
    connection: Option<String>,
    deadline: u64,
}

impl Drop for Attempt {
    fn drop(&mut self) {
        // Volatile writes prevent optimizer removal; no unsafe reference escapes.
        unsafe {
            for byte in self.state.as_bytes_mut() {
                std::ptr::write_volatile(byte, 0);
            }
        }
    }
}

#[derive(Default)]
pub struct Broker {
    attempt: Option<Attempt>,
}

impl Broker {
    fn expire(&mut self, now: u64) {
        if let Some(a) = self.attempt.as_mut() {
            if now >= a.deadline
                && matches!(
                    a.public.status.as_str(),
                    "waiting_browser" | "waiting_helper" | "validating"
                )
            {
                a.public.status = "expired".into();
                a.connection = None;
            }
        }
    }
    pub fn begin(&mut self, provider: &str, now: u64) -> Result<PublicStatus, &'static str> {
        provider_url(provider)?;
        self.attempt = Some(Attempt {
            public: PublicStatus {
                request_id: random_id(),
                provider: provider.into(),
                status: "waiting_browser".into(),
                expires_in: ATTEMPT_SECONDS,
                account: None,
                error_code: None,
            },
            state: random_id(),
            connection: None,
            deadline: now + ATTEMPT_SECONDS,
        });
        self.status(None, now)
    }
    pub fn status(
        &mut self,
        request: Option<&str>,
        now: u64,
    ) -> Result<PublicStatus, &'static str> {
        self.expire(now);
        let Some(a) = &self.attempt else {
            return Ok(PublicStatus {
                request_id: String::new(),
                provider: String::new(),
                status: "idle".into(),
                expires_in: 0,
                account: None,
                error_code: None,
            });
        };
        if request.is_some_and(|id| id != a.public.request_id) {
            return Err("AUTH_UNKNOWN_REQUEST");
        }
        let mut result = a.public.clone();
        result.expires_in = if matches!(
            result.status.as_str(),
            "waiting_browser" | "waiting_helper" | "validating"
        ) {
            a.deadline.saturating_sub(now)
        } else {
            0
        };
        Ok(result)
    }
    pub fn claim(&mut self, connection: &str, now: u64) -> Result<Claim, &'static str> {
        self.expire(now);
        let a = self.attempt.as_mut().ok_or("NO_PENDING_LOGIN")?;
        if a.public.status != "waiting_browser" || a.connection.is_some() {
            return Err("NO_PENDING_LOGIN");
        }
        a.connection = Some(connection.into());
        a.public.status = "waiting_helper".into();
        Ok(Claim {
            request_id: a.public.request_id.clone(),
            provider: a.public.provider.clone(),
            state: a.state.clone(),
        })
    }
    pub fn validate(
        &mut self,
        request: &str,
        provider: &str,
        state: &str,
        connection: &str,
        now: u64,
    ) -> Result<(), &'static str> {
        self.expire(now);
        let a = self.attempt.as_mut().ok_or("NO_PENDING_LOGIN")?;
        let same_state = state.len() == a.state.len()
            && state
                .bytes()
                .zip(a.state.bytes())
                .fold(0u8, |diff, (a, b)| diff | (a ^ b))
                == 0;
        if a.public.status != "waiting_helper"
            || a.public.request_id != request
            || a.public.provider != provider
            || a.connection.as_deref() != Some(connection)
            || !same_state
        {
            return Err("AUTH_INVALID_COMPLETION");
        }
        a.public.status = "validating".into();
        Ok(())
    }
    pub fn can_commit(&mut self, request: &str, now: u64) -> bool {
        self.expire(now);
        self.attempt
            .as_ref()
            .is_some_and(|a| a.public.request_id == request && a.public.status == "validating")
    }
    pub fn succeed(
        &mut self,
        request: &str,
        account: Value,
        now: u64,
    ) -> Result<PublicStatus, &'static str> {
        if !self.can_commit(request, now) {
            return Err("AUTH_CANCELLED");
        }
        let a = self.attempt.as_mut().unwrap();
        a.public.status = "connected".into();
        // Do not forward arbitrary backend response fields into the WebView.
        a.public.account = Some(
            serde_json::json!({"id": account.get("id").and_then(Value::as_str).unwrap_or(""), "name": account.get("name").and_then(Value::as_str).unwrap_or("")}),
        );
        a.connection = None;
        self.status(Some(request), now)
    }
    pub fn fail(&mut self, request: &str, code: &'static str, now: u64) {
        self.expire(now);
        if let Some(a) = self.attempt.as_mut() {
            if a.public.request_id == request
                && matches!(
                    a.public.status.as_str(),
                    "waiting_browser" | "waiting_helper" | "validating"
                )
            {
                a.public.status = "failed".into();
                a.public.error_code = Some(code.into());
                a.connection = None;
            }
        }
    }
    pub fn cancel(&mut self, request: &str, now: u64) -> Result<PublicStatus, &'static str> {
        self.expire(now);
        let a = self.attempt.as_mut().ok_or("NO_PENDING_LOGIN")?;
        if a.public.request_id != request {
            return Err("AUTH_UNKNOWN_REQUEST");
        }
        if matches!(
            a.public.status.as_str(),
            "waiting_browser" | "waiting_helper" | "validating"
        ) {
            a.public.status = "cancelled".into();
            a.connection = None;
        }
        self.status(Some(request), now)
    }
    pub fn disconnected(&mut self, connection: &str) {
        if let Some(a) = self.attempt.as_mut() {
            if a.connection.as_deref() == Some(connection) && a.public.status == "waiting_helper" {
                a.connection = None;
                a.state = random_id();
                a.public.status = "waiting_browser".into();
            }
        }
    }
    pub fn logout(&mut self, provider: &str) {
        if self
            .attempt
            .as_ref()
            .is_some_and(|a| a.public.provider == provider)
        {
            self.attempt = None;
        }
    }
}
