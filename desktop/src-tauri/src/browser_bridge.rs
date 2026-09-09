use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAX_FRAME: usize = 16 * 1024;
pub const FIREFOX_ID: &str = "deckpipe-auth@deckpipe.local";
pub const HOST_NAME: &str = "com.deckpipe.auth";

#[derive(Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum NativeRequest {
    Hello {
        v: u8,
    },
    ClaimPending {
        v: u8,
    },
    Complete {
        v: u8,
        #[serde(rename = "requestId")]
        request_id: String,
        provider: String,
        state: String,
        credential: String,
    },
}
impl NativeRequest {
    pub fn check(&self) -> Result<(), &'static str> {
        let version = match self {
            Self::Hello { v } | Self::ClaimPending { v } | Self::Complete { v, .. } => *v,
        };
        if version != 1 {
            return Err("AUTH_PROTOCOL_VERSION");
        }
        if let Self::Complete {
            request_id,
            provider,
            state,
            credential,
            ..
        } = self
        {
            if request_id.len() != 64
                || state.len() != 64
                || !request_id.bytes().all(|b| b.is_ascii_hexdigit())
                || !state.bytes().all(|b| b.is_ascii_hexdigit())
                || !matches!(provider.as_str(), "sc" | "deezer")
                || credential.is_empty()
                || credential.len() > 8192
                || credential.chars().any(char::is_control)
            {
                return Err("AUTH_INVALID_MESSAGE");
            }
        }
        Ok(())
    }
}

pub fn parse_request(bytes: &[u8]) -> Result<NativeRequest, &'static str> {
    if bytes.is_empty() || bytes.len() > MAX_FRAME {
        return Err("AUTH_INVALID_MESSAGE");
    }
    let request: NativeRequest =
        serde_json::from_slice(bytes).map_err(|_| "AUTH_INVALID_MESSAGE")?;
    request.check()?;
    Ok(request)
}
pub fn encode_frame(value: &Value) -> Result<Vec<u8>, &'static str> {
    let bytes = serde_json::to_vec(value).map_err(|_| "AUTH_INVALID_MESSAGE")?;
    if bytes.len() > MAX_FRAME {
        return Err("AUTH_INVALID_MESSAGE");
    }
    let mut frame = (bytes.len() as u32).to_le_bytes().to_vec();
    frame.extend(bytes);
    Ok(frame)
}
pub fn decode_frame(frame: &[u8]) -> Result<Value, &'static str> {
    if frame.len() < 4 {
        return Err("AUTH_INVALID_MESSAGE");
    }
    let len = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
    if len > MAX_FRAME || len + 4 != frame.len() {
        return Err("AUTH_INVALID_MESSAGE");
    }
    let request = parse_request(&frame[4..])?;
    serde_json::to_value(request).map_err(|_| "AUTH_INVALID_MESSAGE")
}
pub fn valid_host_args(args: &[String]) -> Result<(), &'static str> {
    // Firefox supplies manifest path and stable Gecko ID. Chromium support is
    // disabled until an approved packaged extension ID is pinned here.
    if args.len() == 2
        && args[1] == FIREFOX_ID
        && std::path::Path::new(&args[0])
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("json"))
    {
        Ok(())
    } else {
        Err("AUTH_HOST_SOURCE_REJECTED")
    }
}
pub async fn read_bytes<R: AsyncRead + Unpin>(reader: &mut R) -> Result<Vec<u8>, &'static str> {
    let len = reader.read_u32_le().await.map_err(|_| "AUTH_PIPE_CLOSED")? as usize;
    if len == 0 || len > MAX_FRAME {
        return Err("AUTH_INVALID_MESSAGE");
    }
    let mut bytes = vec![0; len];
    reader
        .read_exact(&mut bytes)
        .await
        .map_err(|_| "AUTH_INVALID_MESSAGE")?;
    Ok(bytes)
}
pub async fn write_value<W: AsyncWrite + Unpin>(
    writer: &mut W,
    value: &Value,
) -> Result<(), &'static str> {
    let frame = encode_frame(value)?;
    writer
        .write_all(&frame)
        .await
        .map_err(|_| "AUTH_PIPE_CLOSED")?;
    writer.flush().await.map_err(|_| "AUTH_PIPE_CLOSED")
}
