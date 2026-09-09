fn main() {
    // Bootstrap the companion EXE before it exists as a Tauri externalBin.
    // This feature is used only with --bin deckpipe-auth-host by its build script.
    if std::env::var_os("CARGO_FEATURE_AUTH_HOST_ONLY").is_some() {
        return;
    }
    tauri_build::build()
}
