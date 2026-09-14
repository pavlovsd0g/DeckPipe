use deckpipe::{
    browser_bridge::{
        parse_request, read_bytes, valid_host_args, write_value, FIREFOX_ID, HOST_NAME,
    },
    native_ipc,
};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

fn verify_manifest(args: &[String]) -> Result<(), &'static str> {
    valid_host_args(args)?;
    let exe = std::env::current_exe().map_err(|_| "AUTH_HOST_SOURCE_REJECTED")?;
    let manifest = Path::new(&args[0]);
    let local_app_data = std::env::var_os("LOCALAPPDATA")
        .map(std::path::PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or("AUTH_HOST_SOURCE_REJECTED")?;
    let expected_manifest = local_app_data
        .join("DeckPipe")
        .join("AuthHelper")
        .join("native-host.firefox.json");
    if !manifest.is_absolute()
        || manifest.canonicalize().ok() != expected_manifest.canonicalize().ok()
        || !exe.with_file_name("deckpipe.exe").is_file()
    {
        return Err("AUTH_HOST_SOURCE_REJECTED");
    }
    let bytes = std::fs::read(manifest).map_err(|_| "AUTH_HOST_SOURCE_REJECTED")?;
    if bytes.len() > 8192 {
        return Err("AUTH_HOST_SOURCE_REJECTED");
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| "AUTH_HOST_SOURCE_REJECTED")?;
    if value.get("name").and_then(Value::as_str) != Some(HOST_NAME)
        || value.get("type").and_then(Value::as_str) != Some("stdio")
        || value.get("allowed_extensions") != Some(&json!([FIREFOX_ID]))
    {
        return Err("AUTH_HOST_SOURCE_REJECTED");
    }
    let configured = Path::new(
        value
            .get("path")
            .and_then(Value::as_str)
            .ok_or("AUTH_HOST_SOURCE_REJECTED")?,
    );
    if configured.canonicalize().ok() != exe.canonicalize().ok() {
        return Err("AUTH_HOST_SOURCE_REJECTED");
    }
    Ok(())
}

async fn run() -> Result<(), &'static str> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    verify_manifest(&args)?;
    let expected = std::env::current_exe()
        .map_err(|_| "AUTH_HOST_UNAVAILABLE")?
        .with_file_name("deckpipe.exe");
    let mut pipe = native_ipc::connect_client(&native_ipc::pipe_name()?, &expected).await?;
    let mut input = tokio::io::stdin();
    let mut output = tokio::io::stdout();
    loop {
        let mut bytes = tokio::time::timeout(Duration::from_secs(305), read_bytes(&mut input))
            .await
            .map_err(|_| "AUTH_EXPIRED")??;
        let request = parse_request(&bytes)?;
        // This host only relays the strict protocol; it has no API bearer,
        // arbitrary-network operation or access to a browser profile.
        let value = serde_json::to_value(request).map_err(|_| "AUTH_INVALID_MESSAGE")?;
        write_value(&mut pipe, &value).await?;
        bytes.fill(0);
        let result = tokio::time::timeout(Duration::from_secs(125), read_bytes(&mut pipe))
            .await
            .map_err(|_| "AUTH_EXPIRED")??;
        let response: Value =
            serde_json::from_slice(&result).map_err(|_| "AUTH_INVALID_MESSAGE")?;
        write_value(&mut output, &response).await?;
    }
}

#[tokio::main]
async fn main() {
    if let Err(code) = run().await {
        let _ = write_value(
            &mut tokio::io::stdout(),
            &json!({"ok":false,"errorCode":code}),
        )
        .await;
    }
    // async stdin uses a blocking reader: explicitly exit after protocol close.
    std::process::exit(0);
}
