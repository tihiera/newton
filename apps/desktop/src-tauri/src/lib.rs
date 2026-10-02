//! Newton desktop shell. All product logic lives in agentd; this shell only tells
//! the webview where agentd is and which bearer token to use.

pub mod agentd;

/// `{ base_url, token, data_dir }`, or `{ code, message }` when agentd hasn't
/// written its token yet. Re-read on every call, so starting agentd after the UI works.
#[tauri::command]
fn agentd_connection() -> Result<agentd::Connection, agentd::ConnectionError> {
    agentd::resolve_from_process()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![agentd_connection])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
