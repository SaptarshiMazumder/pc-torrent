use serde_json::json;
use std::sync::Arc;
use tauri::{AppHandle, State};
use tokio::sync::Mutex;

use crate::persistence::save_agent_state;
use crate::sidecar::{SidecarHandle, spawn_sidecar};
use crate::state::{AgentState, LogEntry};

async fn ensure_sidecar_running(
    app: &AppHandle,
    state: &State<'_, Arc<Mutex<AgentState>>>,
    sidecar: &State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    let handle = sidecar.lock().await;
    if !handle.is_running() {
        drop(handle);
        spawn_sidecar(
            app,
            state.inner().clone(),
            sidecar.inner().clone(),
        )?;
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    Ok(())
}

#[tauri::command]
pub async fn connect_agent(
    app: tauri::AppHandle,
    backend_url: String,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;

    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({
        "cmd": "connect",
        "backend_url": backend_url
    }))?;

    let mut s = state.lock().await;
    s.push_log(LogEntry {
        level: "info".to_string(),
        source: "app".to_string(),
        message: format!("Connecting to {}", backend_url),
    });
    let snapshot = s.clone();
    drop(s);
    if let Err(err) = save_agent_state(&app, &snapshot) {
        eprintln!("[state] {err}");
    }

    Ok(())
}

#[tauri::command]
pub async fn disconnect_agent(
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "disconnect"}))
}

#[tauri::command]
pub async fn pause_agent(
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "pause"}))
}

#[tauri::command]
pub async fn resume_agent(
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "resume"}))
}

#[tauri::command]
pub async fn stop_job(
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "stop_job"}))
}

#[tauri::command]
pub async fn get_agent_state(
    state: State<'_, Arc<Mutex<AgentState>>>,
) -> Result<serde_json::Value, String> {
    let s = state.lock().await;
    Ok(json!({
        "status": s.status,
        "message": s.message,
        "machine_id": s.machine_id,
        "system_info": s.system_info,
        "runtime_info": s.runtime_info,
        "current_job": s.current_job,
        "logs": s.logs.iter().collect::<Vec<_>>()
    }))
}

#[tauri::command]
pub async fn get_system_info(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "get_system_info"}))
}

#[tauri::command]
pub async fn get_runtime_status(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "get_runtime_status"}))
}

#[tauri::command]
pub async fn run_preflight(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "run_preflight"}))
}

#[tauri::command]
pub async fn remove_image(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "remove_image"}))
}
