pub mod http;
pub mod process;
pub mod window;
use tauri::Manager;

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            window::show_window(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet])

        .setup(|app| {
            let pm = process::start_background_processes(app.handle());

            let pm_for_exit = pm.clone();
            app.manage(pm_for_exit);

            let app_handle = app.handle().clone();

            tauri::async_runtime::spawn(async move {
                http::start_server(app_handle).await;
            });

            Ok(())
        })

        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                // Exit時処理（必要なら後で改善）
            }
        });
}