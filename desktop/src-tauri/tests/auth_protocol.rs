use deckpipe::{
    auth_broker::Broker,
    auth_browser::{cookie_is_owned, profile_directory},
};
use std::path::Path;

#[test]
fn persistent_profiles_and_cookie_ownership() {
    let root = Path::new("C:/synthetic-local-app-data");
    assert_eq!(
        profile_directory(root, "deezer").unwrap(),
        root.join("DeckPipe/AuthBrowser/deezer")
    );
    assert_ne!(
        profile_directory(root, "deezer").unwrap(),
        profile_directory(root, "sc").unwrap()
    );
    assert!(cookie_is_owned("deezer", "arl", ".deezer.com"));
    assert!(!cookie_is_owned("deezer", "arl", "evil.deezer.com"));
    assert!(!cookie_is_owned("deezer", "oauth_token", ".soundcloud.com"));
}

#[test]
fn stale_validation_cannot_commit_after_cancel() {
    let mut broker = Broker::default();
    let attempt = broker.begin("deezer", 0).unwrap();
    let window = format!("auth-{}", attempt.request_id);
    let round = broker
        .validate(&attempt.request_id, "deezer", &window, "synthetic_old", 1)
        .unwrap();
    broker.cancel(&attempt.request_id, 2).unwrap();
    assert!(!broker.can_commit(&attempt.request_id, &round, 3));
    let public = serde_json::to_string(&broker.status(None, 3).unwrap()).unwrap();
    assert!(!public.contains("synthetic_old"));
}

#[test]
fn rejects_wrong_provider_window_and_duplicate_submission() {
    let mut b = Broker::default();
    let a = b.begin("sc", 0).unwrap();
    let w = format!("auth-{}", a.request_id);
    assert!(b
        .validate(&a.request_id, "deezer", &w, "synthetic", 1)
        .is_err());
    assert!(b
        .validate(&a.request_id, "sc", "main", "synthetic", 1)
        .is_err());
    assert!(b
        .validate(&a.request_id, "sc", "auth-other", "synthetic", 1)
        .is_err());
    let round = b.validate(&a.request_id, "sc", &w, "synthetic", 1).unwrap();
    assert!(b
        .validate(&a.request_id, "sc", &w, "synthetic-new", 2)
        .is_err());
    assert!(b.can_commit(&a.request_id, &round, 2));
}

#[test]
fn expiry_replacement_logout_reject_late_completion() {
    for action in ["expire", "replace", "logout"] {
        let mut b = Broker::default();
        let a = b.begin("deezer", 0).unwrap();
        let round = b
            .validate(
                &a.request_id,
                "deezer",
                &format!("auth-{}", a.request_id),
                "synthetic",
                1,
            )
            .unwrap();
        match action {
            "expire" => {
                assert_eq!(b.status(None, 300).unwrap().status, "expired");
            }
            "replace" => {
                b.begin("sc", 2).unwrap();
            }
            _ => b.logout("deezer"),
        }
        assert!(!b.can_commit(&a.request_id, &round, 301));
        assert!(b
            .succeed(&a.request_id, &round, serde_json::json!({"id":"a"}), 301)
            .is_err());
    }
}

#[test]
fn rejected_cookie_waits_for_new_material_and_new_round() {
    let mut b = Broker::default();
    let a = b.begin("deezer", 0).unwrap();
    let w = format!("auth-{}", a.request_id);
    let old = b
        .validate(&a.request_id, "deezer", &w, "synthetic-old", 1)
        .unwrap();
    b.rejected(&a.request_id, &old, "AUTH_PROVIDER_REJECTED", 2);
    assert_eq!(b.status(None, 2).unwrap().status, "waiting_browser");
    assert_eq!(
        b.validate(&a.request_id, "deezer", &w, "synthetic-old", 3),
        Err("AUTH_UNCHANGED_CREDENTIAL")
    );
    let new = b
        .validate(&a.request_id, "deezer", &w, "synthetic-new", 4)
        .unwrap();
    assert_ne!(old, new);
    assert!(!b.can_commit(&a.request_id, &old, 5));
    assert!(b.can_commit(&a.request_id, &new, 5));
    b.rejected(&a.request_id, &old, "AUTH_PROVIDER_REJECTED", 6);
    assert_eq!(b.status(None, 6).unwrap().status, "validating");
}

#[test]
fn logout_is_selective_and_public_values_are_sanitized() {
    let mut b = Broker::default();
    let a = b.begin("sc", 0).unwrap();
    let round = b
        .validate(
            &a.request_id,
            "sc",
            &format!("auth-{}", a.request_id),
            "credential-sentinel",
            1,
        )
        .unwrap();
    b.logout("deezer");
    assert!(b.can_commit(&a.request_id, &round, 2));
    let public = b
        .succeed(
            &a.request_id,
            &round,
            serde_json::json!({"id":"safe", "name":"Safe", "token":"credential-sentinel"}),
            3,
        )
        .unwrap();
    let serialized = serde_json::to_string(&public).unwrap();
    assert!(!serialized.contains("credential-sentinel"));
    assert!(!serialized.contains(&round));
    assert!(!serialized.contains("token"));
    assert_eq!(public.status, "connected");
}

#[test]
fn navigation_denies_local_and_executable_targets() {
    use deckpipe::auth_browser::navigation_allowed;
    for url in [
        "http://www.deezer.com",
        "file:///C:/test",
        "tauri://localhost",
        "javascript:alert(1)",
        "https://localhost:7100",
        "https://localhost.:7100",
        "https://127.0.0.1:7100",
        "https://[::1]:7100",
        "https://[::ffff:127.0.0.1]",
        "https://192.168.1.1",
        "https://2130706433",
        "https://user:password@example.com",
        "https://host.local",
    ] {
        assert!(!navigation_allowed(&url.parse().unwrap()), "{url}");
    }
    for url in [
        "https://www.deezer.com/login",
        "https://soundcloud.com/sign-in",
        "https://accounts.google.com/login",
        "https://www.facebook.com/login",
    ] {
        assert!(navigation_allowed(&url.parse().unwrap()), "{url}");
    }
}

#[test]
fn cookie_scope_never_accepts_similar_domains_or_names() {
    for domain in [
        "deezer.com.evil.test",
        "evil.deezer.com",
        "..deezer.com",
        "soundcloud.com",
        "",
        "com",
    ] {
        assert!(!cookie_is_owned("deezer", "arl", domain));
    }
    assert!(cookie_is_owned("sc", "oauth_token", ".soundcloud.com"));
    assert!(!cookie_is_owned("sc", "oauth_token", "api.soundcloud.com"));
    assert!(!cookie_is_owned("sc", "arl", ".soundcloud.com"));
    assert!(profile_directory(Path::new("relative"), "sc").is_err());
    assert!(profile_directory(Path::new("C:/synthetic"), "../main").is_err());
}

#[test]
fn actual_capability_excludes_every_remote_auth_window() {
    // Exercise the packaged ACL configuration, not a duplicate policy fixture.
    // All invoke commands additionally require the native window label main.
    let capability: serde_json::Value =
        serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
    assert_eq!(capability["windows"], serde_json::json!(["main"]));
    assert!(capability.get("remote").is_none());
    assert!(capability.get("webviews").is_none());
    assert_eq!(
        capability["permissions"],
        serde_json::json!(["allow-auth-broker", "allow-backend-connection"])
    );
}
