mod receiver_sidecar;

use tauri::{Manager, RunEvent};

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(receiver_sidecar::ReceiverState::default())
        .setup(|app| {
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let Some(window) = app_handle.get_webview_window("main") else {
                    return;
                };
                match receiver_sidecar::start_receiver_sidecar(&app_handle).await {
                    Ok(url) => {
                        let _ = receiver_sidecar::navigate_main_window(&window, &url);
                    }
                    Err(error) => {
                        let _ =
                            receiver_sidecar::render_startup_error(&window, &format!("{error:#}"));
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Receiver desktop shell")
        .run(|app_handle, event| match event {
            RunEvent::ExitRequested { .. } | RunEvent::Exit => {
                receiver_sidecar::cleanup_receiver_sidecar(app_handle);
            }
            _ => {}
        });
}
