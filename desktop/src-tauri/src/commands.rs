use serde_json::json;
use serde::Serialize;
use reqwest::header::CONTENT_DISPOSITION;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tauri::{AppHandle, State};
use tokio::sync::Mutex;

use crate::persistence::save_agent_state;
use crate::sidecar::{SidecarHandle, spawn_sidecar};
use crate::state::{AgentState, LogEntry};

#[derive(Serialize)]
pub struct DownloadResult {
    pub path: String,
    pub filename: String,
}

fn downloads_dir() -> Result<PathBuf, String> {
    if let Ok(profile) = std::env::var("USERPROFILE") {
        return Ok(PathBuf::from(profile).join("Downloads"));
    }

    if let Ok(home) = std::env::var("HOME") {
        return Ok(PathBuf::from(home).join("Downloads"));
    }

    Err("Could not resolve the Downloads folder.".to_string())
}

fn sanitize_filename(name: &str) -> String {
    let raw = Path::new(name)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("download.bin");

    raw.chars()
        .map(|ch| match ch {
            '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*' => '_',
            _ => ch,
        })
        .collect()
}

fn parse_download_filename(header: &str) -> Option<String> {
    for part in header.split(';') {
        let trimmed = part.trim();
        if let Some(value) = trimmed.strip_prefix("filename*=UTF-8''") {
            return Some(value.replace("%20", " "));
        }
        if let Some(value) = trimmed.strip_prefix("filename=") {
            return Some(value.trim_matches('"').to_string());
        }
    }

    None
}

fn filename_from_url(url: &str) -> Option<String> {
    url.split('?')
        .next()
        .and_then(|value| value.rsplit('/').next())
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
}

fn unique_download_path(dir: &Path, filename: &str) -> PathBuf {
    let sanitized = sanitize_filename(filename);
    let candidate = dir.join(&sanitized);
    if !candidate.exists() {
        return candidate;
    }

    let file_path = Path::new(&sanitized);
    let stem = file_path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("download");
    let ext = file_path
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| format!(".{value}"))
        .unwrap_or_default();

    for index in 1..1000 {
        let candidate = dir.join(format!("{stem} ({index}){ext}"));
        if !candidate.exists() {
            return candidate;
        }
    }

    dir.join(format!("{stem}-copy{ext}"))
}

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

#[tauri::command]
pub async fn download_job_output_to_downloads(
    url: String,
) -> Result<DownloadResult, String> {
    let client = reqwest::Client::new();
    let mut response = client
        .get(&url)
        .send()
        .await
        .map_err(|err| format!("Download request failed: {err}"))?;

    if !response.status().is_success() {
        let status = response.status();
        let detail = response.text().await.unwrap_or_default();
        return Err(if detail.trim().is_empty() {
            format!("Download failed with status {status}")
        } else {
            format!("Download failed with status {status}: {detail}")
        });
    }

    let filename = response
        .headers()
        .get(CONTENT_DISPOSITION)
        .and_then(|value| value.to_str().ok())
        .and_then(parse_download_filename)
        .or_else(|| filename_from_url(response.url().as_str()))
        .unwrap_or_else(|| "download.bin".to_string());

    let downloads = downloads_dir()?;
    fs::create_dir_all(&downloads)
        .map_err(|err| format!("Failed to create Downloads folder: {err}"))?;

    let file_path = unique_download_path(&downloads, &filename);
    let mut file = File::create(&file_path)
        .map_err(|err| format!("Failed to create download file: {err}"))?;

    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|err| format!("Failed while downloading file: {err}"))?
    {
        file.write_all(&chunk)
            .map_err(|err| format!("Failed to write download file: {err}"))?;
    }

    Ok(DownloadResult {
        path: file_path.to_string_lossy().to_string(),
        filename,
    })
}
