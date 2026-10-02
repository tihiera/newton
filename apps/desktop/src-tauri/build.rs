fn main() {
    // Declaring the app's commands puts them under Tauri's capability ACL: a window
    // can call `agentd_connection` / `save_file` only if capabilities/default.json allows it.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["agentd_connection", "save_file"]),
    ))
    .expect("failed to run tauri-build");
}
