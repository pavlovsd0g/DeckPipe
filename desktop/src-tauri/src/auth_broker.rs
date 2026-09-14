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

impl PublicStatus {
    pub fn idle(provider: &str) -> Self {
        Self {
            request_id: String::new(),
            provider: provider.into(),
            status: "idle".into(),
            expires_in: 0,
            account: None,
            error_code: None,
        }
    }
    pub fn pending(&self) -> bool {
        matches!(self.status.as_str(), "waiting_browser" | "validating")
    }
}

// Retain only the last submitted material, privately, to avoid retry storms.
// It is never Debug/Serialize and is erased on replacement/cancel/expiry.
struct Material(String);
impl Drop for Material {
    fn drop(&mut self) {
        unsafe {
            for byte in self.0.as_bytes_mut() {
                std::ptr::write_volatile(byte, 0);
            }
        }
    }
}
struct Attempt {
    public: PublicStatus,
    deadline: u64,
    round: Option<String>,
    last_material: Option<Material>,
}
#[derive(Default)]
pub struct Broker {
    attempt: Option<Attempt>,
}

impl Broker {
    fn expire(&mut self, now: u64) {
        if let Some(a) = self.attempt.as_mut() {
            if now >= a.deadline && a.public.pending() {
                a.public.status = "expired".into();
                a.last_material = None;
            }
        }
    }
    pub fn active_round(&self) -> Option<String> {
        self.attempt.as_ref().and_then(|a| a.round.clone())
    }
    pub fn take_round(&mut self) -> Option<String> {
        self.attempt.as_mut().and_then(|a| a.round.take())
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
            deadline: now + ATTEMPT_SECONDS,
            round: None,
            last_material: None,
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
            return if request.is_some() {
                Err("AUTH_UNKNOWN_REQUEST")
            } else {
                Ok(PublicStatus::idle(""))
            };
        };
        if request.is_some_and(|id| id != a.public.request_id) {
            return Err("AUTH_UNKNOWN_REQUEST");
        }
        let mut result = a.public.clone();
        result.expires_in = if result.pending() {
            a.deadline.saturating_sub(now)
        } else {
            0
        };
        Ok(result)
    }
    pub fn validate(
        &mut self,
        request: &str,
        provider: &str,
        window: &str,
        credential: &str,
        now: u64,
    ) -> Result<String, &'static str> {
        self.expire(now);
        let a = self.attempt.as_mut().ok_or("NO_PENDING_LOGIN")?;
        if a.public.status != "waiting_browser"
            || a.public.request_id != request
            || a.public.provider != provider
            || window != format!("auth-{request}")
        {
            return Err("AUTH_INVALID_COMPLETION");
        }
        if credential.is_empty() || credential.len() > 8192 || credential.trim() != credential {
            return Err("AUTH_INVALID_CREDENTIAL");
        }
        if a.last_material
            .as_ref()
            .is_some_and(|last| last.0 == credential)
        {
            return Err("AUTH_UNCHANGED_CREDENTIAL");
        }
        let round = random_id();
        a.round = Some(round.clone());
        a.last_material = Some(Material(credential.into()));
        a.public.status = "validating".into();
        a.public.error_code = None;
        Ok(round)
    }
    pub fn can_commit(&mut self, request: &str, round: &str, now: u64) -> bool {
        self.expire(now);
        self.attempt.as_ref().is_some_and(|a| {
            a.public.request_id == request
                && a.public.status == "validating"
                && a.round.as_deref() == Some(round)
        })
    }
    pub fn rejected(&mut self, request: &str, round: &str, code: &'static str, now: u64) {
        if self.can_commit(request, round, now) {
            let a = self.attempt.as_mut().unwrap();
            a.public.status = "waiting_browser".into();
            a.public.error_code = Some(code.into());
            a.round = None;
        }
    }
    pub fn succeed(
        &mut self,
        request: &str,
        round: &str,
        account: Value,
        now: u64,
    ) -> Result<PublicStatus, &'static str> {
        if !self.can_commit(request, round, now) {
            return Err("AUTH_CANCELLED");
        }
        let a = self.attempt.as_mut().unwrap();
        a.public.status = "connected".into();
        a.public.account = Some(
            serde_json::json!({"id":account.get("id").and_then(Value::as_str).unwrap_or(""), "name":account.get("name").and_then(Value::as_str).unwrap_or("")}),
        );
        a.last_material = None;
        a.round = None;
        self.status(Some(request), now)
    }
    pub fn notice(&mut self, request: &str, code: &'static str, now: u64) {
        self.expire(now);
        if let Some(a) = self.attempt.as_mut() {
            if a.public.request_id == request && a.public.pending() {
                a.public.error_code = Some(code.into());
            }
        }
    }
    pub fn fail(&mut self, request: &str, code: &'static str, now: u64) {
        self.expire(now);
        if let Some(a) = self.attempt.as_mut() {
            if a.public.request_id == request && a.public.pending() {
                a.public.status = "failed".into();
                a.public.error_code = Some(code.into());
                a.last_material = None;
            }
        }
    }
    pub fn cancel(&mut self, request: &str, now: u64) -> Result<PublicStatus, &'static str> {
        self.expire(now);
        let a = self.attempt.as_mut().ok_or("NO_PENDING_LOGIN")?;
        if a.public.request_id != request {
            return Err("AUTH_UNKNOWN_REQUEST");
        }
        if a.public.pending() {
            a.public.status = "cancelled".into();
            a.last_material = None;
        }
        self.status(Some(request), now)
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
