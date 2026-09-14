use deckpipe::auth_broker::Broker;
use deckpipe::browser_bridge::{decode_frame, encode_frame, valid_host_args};

#[test]
fn state_is_private_and_completion_is_bound_to_claim() {
    let mut broker = Broker::default();
    let public = broker.begin("sc", 10).unwrap();
    assert!(serde_json::to_string(&public)
        .unwrap()
        .find("state")
        .is_none());
    let claim = broker.claim("connection", 11).unwrap();
    assert!(broker
        .validate(&public.request_id, "sc", &claim.state, "other", 12)
        .is_err());
    broker
        .validate(&public.request_id, "sc", &claim.state, "connection", 12)
        .unwrap();
    assert!(broker
        .validate(&public.request_id, "sc", &claim.state, "connection", 12)
        .is_err());
}

#[test]
fn cancellation_expiry_and_replacement_reject_old_callbacks() {
    let mut broker = Broker::default();
    let first = broker.begin("deezer", 0).unwrap();
    let claim = broker.claim("connection", 1).unwrap();
    broker.cancel(&first.request_id, 2).unwrap();
    assert!(broker
        .validate(&first.request_id, "deezer", &claim.state, "connection", 3)
        .is_err());
    broker.begin("sc", 5).unwrap();
    assert!(broker.claim("connection", 306).is_err());
    assert_eq!(broker.status(None, 306).unwrap().status, "expired");
    assert!(broker.begin("unknown", 310).is_err());
}

#[test]
fn native_frames_and_sources_are_strict() {
    let value = serde_json::json!({"v":1,"op":"hello"});
    let frame = encode_frame(&value).unwrap();
    assert_eq!(decode_frame(&frame).unwrap(), value);
    assert!(decode_frame(&[255, 255, 255, 255]).is_err());
    assert!(decode_frame(&frame[..frame.len() - 1]).is_err());
    let duplicate = br#"{"v":1,"v":1,"op":"hello"}"#;
    let mut frame = (duplicate.len() as u32).to_le_bytes().to_vec();
    frame.extend(duplicate);
    assert!(decode_frame(&frame).is_err());
    assert!(valid_host_args(&["manifest.json".into(), "other@example.org".into()]).is_err());
    assert!(valid_host_args(&[
        "manifest.json".into(),
        "deckpipe-auth@deckpipe.local".into()
    ])
    .is_ok());
}

#[cfg(windows)]
#[tokio::test]
async fn actual_named_pipe_roundtrip_checks_peer_and_frames() {
    use deckpipe::browser_bridge::{read_bytes, write_value};
    use deckpipe::native_ipc;
    let name = format!(
        "{}-test-{}",
        native_ipc::pipe_name().unwrap(),
        std::process::id()
    );
    let mut server = native_ipc::create_server(&name, true).unwrap();
    assert!(native_ipc::create_server(&name, true).is_err());
    let executable = std::env::current_exe().unwrap();
    let expected = executable.clone();
    let client = tokio::spawn(async move {
        let mut pipe = native_ipc::connect_client(&name, &expected).await.unwrap();
        write_value(&mut pipe, &serde_json::json!({"v":1,"op":"hello"}))
            .await
            .unwrap();
        serde_json::from_slice::<serde_json::Value>(&read_bytes(&mut pipe).await.unwrap()).unwrap()
    });
    server.connect().await.unwrap();
    native_ipc::verify_client(&server, &executable).unwrap();
    let request = read_bytes(&mut server).await.unwrap();
    assert!(deckpipe::browser_bridge::parse_request(&request).is_ok());
    write_value(&mut server, &serde_json::json!({"ok":true}))
        .await
        .unwrap();
    assert_eq!(client.await.unwrap(), serde_json::json!({"ok":true}));
}

#[test]
fn cancelled_validation_and_wrong_provider_never_commit() {
    let mut broker = Broker::default();
    let pending = broker.begin("sc", 0).unwrap();
    let claim = broker.claim("connection", 1).unwrap();
    assert!(broker
        .validate(&pending.request_id, "deezer", &claim.state, "connection", 2)
        .is_err());
    broker
        .validate(&pending.request_id, "sc", &claim.state, "connection", 2)
        .unwrap();
    broker.cancel(&pending.request_id, 3).unwrap();
    assert!(!broker.can_commit(&pending.request_id, 4));
    assert!(broker
        .succeed(&pending.request_id, serde_json::json!({"id":"42"}), 4)
        .is_err());
    let second = broker.begin("deezer", 10).unwrap();
    let claim = broker.claim("new", 11).unwrap();
    broker
        .validate(&second.request_id, "deezer", &claim.state, "new", 12)
        .unwrap();
    assert!(!broker.can_commit(&second.request_id, 311));
}

#[test]
fn actual_native_host_rejects_untrusted_manifest_inputs_before_ipc() {
    use std::{path::PathBuf, process::Command};

    fn assert_rejected(
        host: &PathBuf,
        manifest: &PathBuf,
        local_app_data: &PathBuf,
        extension_id: &str,
    ) {
        let output = Command::new(host)
            .arg(manifest)
            .arg(extension_id)
            .env("LOCALAPPDATA", local_app_data)
            .output()
            .unwrap();
        assert!(output.status.success());
        assert!(output.stderr.is_empty());
        assert!(!String::from_utf8_lossy(&output.stdout).contains("synthetic-secret"));
        assert!(output.stdout.len() >= 4);
        let response: serde_json::Value = serde_json::from_slice(&output.stdout[4..]).unwrap();
        assert_eq!(
            response,
            serde_json::json!({"ok":false,"errorCode":"AUTH_HOST_SOURCE_REJECTED"})
        );
    }

    let root = PathBuf::from(
        std::env::var("DECKPIPE_AUTH_TEST_DIR").expect("isolated lab test directory is required"),
    )
    .join(format!(
        "native-host-rejections-{}",
        deckpipe::auth_broker::random_id()
    ));
    let install = root.join("install");
    let local_app_data = root.join("local-app-data");
    std::fs::create_dir_all(&install).unwrap();
    let host = install.join("deckpipe-auth-host.exe");
    std::fs::copy(env!("CARGO_BIN_EXE_deckpipe-auth-host"), &host).unwrap();
    std::fs::copy(
        std::env::current_exe().unwrap(),
        install.join("deckpipe.exe"),
    )
    .unwrap();
    let expected = local_app_data
        .join("DeckPipe")
        .join("AuthHelper")
        .join("native-host.firefox.json");
    std::fs::create_dir_all(expected.parent().unwrap()).unwrap();
    let valid = serde_json::json!({"name":"com.deckpipe.auth","type":"stdio","path":host,"allowed_extensions":["deckpipe-auth@deckpipe.local"]});

    let arbitrary = root.join("arbitrary.json");
    std::fs::write(&arbitrary, serde_json::to_vec(&valid).unwrap()).unwrap();
    assert_rejected(
        &host,
        &arbitrary,
        &local_app_data,
        "deckpipe-auth@deckpipe.local",
    );

    let mismatched = serde_json::json!({"name":"com.deckpipe.auth","type":"stdio","path":root.join("other.exe"),"allowed_extensions":["deckpipe-auth@deckpipe.local"],"credential":"synthetic-secret"});
    std::fs::write(&expected, serde_json::to_vec(&mismatched).unwrap()).unwrap();
    assert_rejected(
        &host,
        &expected,
        &local_app_data,
        "deckpipe-auth@deckpipe.local",
    );

    std::fs::write(
        &expected,
        br#"{"name":"com.deckpipe.auth","credential":"synthetic-secret""#,
    )
    .unwrap();
    assert_rejected(
        &host,
        &expected,
        &local_app_data,
        "deckpipe-auth@deckpipe.local",
    );

    let oversized = serde_json::json!({"name":"com.deckpipe.auth","type":"stdio","path":host,"allowed_extensions":["deckpipe-auth@deckpipe.local"],"padding":"x".repeat(8200)});
    assert!(serde_json::to_vec(&oversized).unwrap().len() > 8192);
    std::fs::write(&expected, serde_json::to_vec(&oversized).unwrap()).unwrap();
    assert_rejected(
        &host,
        &expected,
        &local_app_data,
        "deckpipe-auth@deckpipe.local",
    );

    std::fs::write(&expected, serde_json::to_vec(&valid).unwrap()).unwrap();
    assert_rejected(&host, &expected, &local_app_data, "other@example.invalid");
}

#[tokio::test]
async fn native_host_stdio_roundtrip() {
    use deckpipe::{
        browser_bridge::{read_bytes, write_value},
        native_ipc,
    };
    use std::{path::PathBuf, process::Stdio, time::Duration};
    if std::env::var("DECKPIPE_NATIVE_TEST_INNER").as_deref() != Ok("1") {
        let root = PathBuf::from(
            std::env::var("DECKPIPE_AUTH_TEST_DIR")
                .expect("isolated lab test directory is required"),
        );
        let directory = root.join(format!(
            "native-host-{}",
            deckpipe::auth_broker::random_id()
        ));
        let local_app_data = directory.join("isolated-local-app-data");
        std::fs::create_dir_all(&directory).unwrap();
        let app = directory.join("deckpipe.exe");
        std::fs::copy(std::env::current_exe().unwrap(), &app).unwrap();
        std::fs::copy(
            env!("CARGO_BIN_EXE_deckpipe-auth-host"),
            directory.join("deckpipe-auth-host.exe"),
        )
        .unwrap();
        let mut command = tokio::process::Command::new(&app);
        command
            .arg("--exact")
            .arg("native_host_stdio_roundtrip")
            .env("DECKPIPE_NATIVE_TEST_INNER", "1")
            .env("LOCALAPPDATA", &local_app_data)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        let output = tokio::time::timeout(Duration::from_secs(20), command.output())
            .await
            .unwrap()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
        return;
    }
    let app = std::env::current_exe().unwrap();
    let directory = app.parent().unwrap();
    let host = directory.join("deckpipe-auth-host.exe");
    let manifest = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .expect("isolated LocalAppData is required")
        .join("DeckPipe")
        .join("AuthHelper")
        .join("native-host.firefox.json");
    std::fs::create_dir_all(manifest.parent().unwrap()).unwrap();
    std::fs::write(&manifest,serde_json::to_vec(&serde_json::json!({"name":"com.deckpipe.auth","type":"stdio","path":host,"allowed_extensions":["deckpipe-auth@deckpipe.local"]})).unwrap()).unwrap();
    let mut server = native_ipc::create_server(&native_ipc::pipe_name().unwrap(), true).unwrap();
    let mut child = tokio::process::Command::new(&host)
        .arg(&manifest)
        .arg("deckpipe-auth@deckpipe.local")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), server.connect())
        .await
        .unwrap()
        .unwrap();
    native_ipc::verify_client(&server, &host).unwrap();
    let mut input = child.stdin.take().unwrap();
    let mut output = child.stdout.take().unwrap();
    write_value(&mut input, &serde_json::json!({"v":1,"op":"hello"}))
        .await
        .unwrap();
    let request = tokio::time::timeout(Duration::from_secs(5), read_bytes(&mut server))
        .await
        .unwrap()
        .unwrap();
    assert!(deckpipe::browser_bridge::parse_request(&request).is_ok());
    write_value(
        &mut server,
        &serde_json::json!({"ok":true,"status":{"provider":"sc","status":"waiting_browser"}}),
    )
    .await
    .unwrap();
    let reply = tokio::time::timeout(Duration::from_secs(5), read_bytes(&mut output))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&reply).unwrap()["ok"],
        true
    );
    // Actual executable rejects a malformed credential-bearing frame without echo.
    write_value(
        &mut input,
        &serde_json::json!({"v":1,"op":"complete","credential":"synthetic-secret"}),
    )
    .await
    .unwrap();
    let rejected = tokio::time::timeout(Duration::from_secs(5), read_bytes(&mut output))
        .await
        .unwrap()
        .unwrap();
    assert!(!String::from_utf8_lossy(&rejected).contains("synthetic-secret"));
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&rejected).unwrap()["ok"],
        false
    );
    drop(input);
    tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .unwrap()
        .unwrap();
}
