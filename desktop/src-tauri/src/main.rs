// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod persistence;
mod sidecar;
mod state;

use std::sync::Arc;
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
    tray::TrayIconBuilder,
    Manager,
};
use tokio::sync::Mutex;

use sidecar::SidecarHandle;
use state::AgentState;

fn main() {
    let agent_state = Arc::new(Mutex::new(AgentState::default()));
    let sidecar_handle = Arc::new(Mutex::new(SidecarHandle::new()));
    let startup_state = agent_state.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(agent_state.clone())
        .manage(sidecar_handle.clone())
        .invoke_handler(tauri::generate_handler![
            commands::connect_agent,
            commands::disconnect_agent,
            commands::pause_agent,
            commands::resume_agent,
            commands::stop_job,
            commands::get_agent_state,
            commands::get_system_info,
            commands::get_runtime_status,
            commands::run_preflight,
            commands::remove_image,
        ])
        .setup(move |app| {
            match persistence::load_agent_state(&app.handle()) {
                Ok(mut saved_state) => {
                    saved_state.status = "disconnected".to_string();
                    saved_state.message = "Ready".to_string();
                    saved_state.current_job = None;
                    saved_state.runtime_info.preflight_complete = false;
                    saved_state.runtime_info.preflight_passed = None;
                    saved_state.runtime_info.preflight_message = "Preflight is pending this launch.".to_string();
                    tauri::async_runtime::block_on(async {
                        *startup_state.lock().await = saved_state;
                    });
                }
                Err(err) => eprintln!("[state] {err}"),
            }

            // Build system tray
            let show = MenuItemBuilder::with_id("show", "Show Window").build(app)?;
            let pause = MenuItemBuilder::with_id("pause", "Pause Agent").build(app)?;
            let stop = MenuItemBuilder::with_id("stop", "Stop Job").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&show)
                .separator()
                .item(&pause)
                .item(&stop)
                .separator()
                .item(&quit)
                .build()?;

            let _tray = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("PC Rent Agent")
                .on_menu_event(move |app, event| {
                    match event.id().as_ref() {
                        "show" => {
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                        "pause" => {
                            let sidecar = app.state::<Arc<Mutex<SidecarHandle>>>();
                            let sidecar = sidecar.inner().clone();
                            tauri::async_runtime::spawn(async move {
                                let mut handle = sidecar.lock().await;
                                let _ = handle.send_command(
                                    &serde_json::json!({"cmd": "pause"}),
                                );
                            });
                        }
                        "stop" => {
                            let sidecar = app.state::<Arc<Mutex<SidecarHandle>>>();
                            let sidecar = sidecar.inner().clone();
                            tauri::async_runtime::spawn(async move {
                                let mut handle = sidecar.lock().await;
                                let _ = handle.send_command(
                                    &serde_json::json!({"cmd": "stop_job"}),
                                );
                            });
                        }
                        "quit" => {
                            let sidecar = app.state::<Arc<Mutex<SidecarHandle>>>();
                            let sidecar = sidecar.inner().clone();
                            tauri::async_runtime::spawn(async move {
                                let mut handle = sidecar.lock().await;
                                let _ = handle.send_command(
                                    &serde_json::json!({"cmd": "disconnect"}),
                                );
                                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                                handle.kill();
                            });
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::DoubleClick { .. } = event {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            // Minimize to tray on close
            let window = app.get_webview_window("main").unwrap();
            let window_clone = window.clone();
            window.on_window_event(move |event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window_clone.hide();
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
