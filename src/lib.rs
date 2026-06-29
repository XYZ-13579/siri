pub mod http;
pub mod process;
pub mod window;

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let pm = process::start_background_processes();
    let pm_for_exit = pm.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            window::show_window(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet])
        .setup(move |app| {
            let app_handle = app.handle().clone();
            
            // Start HTTP server for listener
            tauri::async_runtime::spawn(async move {
                http::start_server(app_handle).await;
            });
            
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                let mut manager = pm_for_exit.lock().unwrap();
                // listener と llama-server はバックグラウンドで継続させる
                manager.on_assistant_exit();
            }
        });
}
