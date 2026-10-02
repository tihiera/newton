//! Newton desktop shell. All product logic lives in agentd; this shell only tells
//! the webview where agentd is and which bearer token to use, opens allowed links in
//! the browser (capabilities/default.json scopes them) and saves downloaded files.

pub mod agentd;
pub mod files;

use tauri::ipc::Request;
use tauri_plugin_dialog::DialogExt;

/// `{ base_url, token, data_dir }`, or `{ code, message }` when agentd hasn't
/// written its token yet. Re-read on every call, so starting agentd after the UI works.
#[tauri::command]
fn agentd_connection() -> Result<agentd::Connection, agentd::ConnectionError> {
    agentd::resolve_from_process()
}

/// Asks where to save the raw body (the suggested name in `files::NAME_HEADER`) with a
/// native save dialog and writes it there. The picked path, or null when cancelled.
#[tauri::command]
async fn save_file(
    window: tauri::WebviewWindow,
    request: Request<'_>,
) -> Result<Option<String>, String> {
    let (bytes, name) =
        files::save_request(request.body(), request.headers()).map_err(|e| e.to_string())?;
    // The dialog blocks until the user answers: not on the async runtime's threads.
    let saved = tauri::async_runtime::spawn_blocking(move || {
        let picked = window
            .dialog()
            .file()
            .set_parent(&window)
            .set_file_name(name)
            .blocking_save_file();
        let Some(picked) = picked else {
            return Ok(None);
        };
        let path = picked
            .into_path()
            .map_err(|e| format!("can't save there: {e}"))?;
        std::fs::write(&path, &bytes)
            .map_err(|e| format!("couldn't write {}: {e}", path.display()))?;
        Ok(Some(path.display().to_string()))
    });
    saved.await.map_err(|e| e.to_string())?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![agentd_connection, save_file])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
