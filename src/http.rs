use axum::{routing::post, Router};
use std::net::SocketAddr;
use tauri::AppHandle;

pub async fn start_server(app_handle: AppHandle) {
    let app = Router::new()
        .route("/show", post({
            let app_handle = app_handle.clone();
            move || async move {
                crate::window::show_window(&app_handle);
                "OK"
            }
        }));

    let addr = SocketAddr::from(([127, 0, 0, 1], 5678));
    println!("[HTTP] Listening on {}", addr);
    if let Ok(listener) = tokio::net::TcpListener::bind(&addr).await {
        let _ = axum::serve(listener, app).await;
    } else {
        eprintln!("[HTTP] Failed to bind to {}", addr);
    }
}
