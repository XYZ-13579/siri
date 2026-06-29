use tauri::{AppHandle, Manager};

pub fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        if let Ok(is_minimized) = window.is_minimized() {
            if is_minimized {
                let _ = window.unminimize();
            }
        }
        let _ = window.show();
        let _ = window.set_focus();
    }
}
