use serde::{Deserialize, Serialize};
use serde_json::json;
use reqwest::header::CONTENT_DISPOSITION;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, State};
use tauri_plugin_shell::ShellExt;
use tokio::io::AsyncReadExt;
use tokio::sync::Mutex;

use crate::persistence::save_agent_state;
use crate::sidecar::{SidecarHandle, spawn_sidecar};
use crate::state::{AgentState, LogEntry};

#[derive(Serialize)]
pub struct DownloadResult {
    pub path: String,
    pub filename: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProjectFileSelection {
    pub path: String,
    pub name: String,
    pub size: u64,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ProjectAnalysisResult {
    pub ok: bool,
    pub frame_start: Option<i64>,
    pub frame_end: Option<i64>,
    pub frame_step: Option<i64>,
    pub total_frames: Option<i64>,
    pub method: String,
    pub error: Option<String>,
    pub parser_error: Option<String>,
    pub blender_error: Option<String>,
    pub blender_path: Option<String>,
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

fn validate_project_extension(path: &Path) -> Result<(), String> {
    let ext = path
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| value.to_ascii_lowercase())
        .ok_or_else(|| "Selected file must be .blend or .zip".to_string())?;

    if ext == "blend" || ext == "zip" {
        Ok(())
    } else {
        Err("Selected file must be .blend or .zip".to_string())
    }
}

#[cfg(target_os = "windows")]
fn pick_project_file_windows() -> Result<Option<PathBuf>, String> {
    let script = "$ErrorActionPreference = 'Stop'; \
Add-Type -AssemblyName System.Windows.Forms; \
$dialog = New-Object System.Windows.Forms.OpenFileDialog; \
$dialog.Filter = 'Blender Project (*.blend;*.zip)|*.blend;*.zip'; \
$dialog.Title = 'Select Blender Project'; \
$dialog.Multiselect = $false; \
$dialog.CheckFileExists = $true; \
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dialog.FileName }";

    let output = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ])
        .output()
        .map_err(|err| format!("Failed to open native file picker: {err}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if stderr.is_empty() {
            "Failed to open native file picker".to_string()
        } else {
            format!("Failed to open native file picker: {stderr}")
        });
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let picked = stdout
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty());

    Ok(picked.map(PathBuf::from))
}

fn emit_upload_progress(app: &AppHandle, uploaded_bytes: u64, total_bytes: u64) {
    let progress_pct = if total_bytes > 0 {
        ((uploaded_bytes as f64 / total_bytes as f64) * 100.0).round()
    } else {
        0.0
    };
    let _ = app.emit(
        "project-upload-progress",
        json!({
            "uploadedBytes": uploaded_bytes,
            "totalBytes": total_bytes,
            "progressPct": progress_pct.max(0.0).min(100.0),
        }),
    );
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
pub async fn run_wsl_setup(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "run_wsl_setup"}))
}

#[tauri::command]
pub async fn run_docker_setup(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "run_docker_setup"}))
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
pub async fn pick_project_file() -> Result<Option<ProjectFileSelection>, String> {
    #[cfg(not(target_os = "windows"))]
    {
        Err("Native project picker is only implemented on Windows.".to_string())
    }

    #[cfg(target_os = "windows")]
    {
        let selected_path = tokio::task::spawn_blocking(pick_project_file_windows)
            .await
            .map_err(|err| format!("Failed to join file picker task: {err}"))??;

        let Some(path) = selected_path else {
            return Ok(None);
        };

        validate_project_extension(&path)?;

        let metadata = fs::metadata(&path)
            .map_err(|err| format!("Failed to read selected file metadata: {err}"))?;
        let name = path
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or_else(|| "Selected file has an invalid filename.".to_string())?
            .to_string();

        Ok(Some(ProjectFileSelection {
            path: path.to_string_lossy().to_string(),
            name,
            size: metadata.len(),
        }))
    }
}

#[tauri::command]
pub async fn analyze_project_file(
    app: tauri::AppHandle,
    file_path: String,
) -> Result<ProjectAnalysisResult, String> {
    let input_path = PathBuf::from(&file_path);
    if !input_path.exists() {
        return Err("Selected file no longer exists.".to_string());
    }
    validate_project_extension(&input_path)?;

    let shell = app.shell();
    let output = shell
        .sidecar("pcrent-agent")
        .map_err(|err| format!("Failed to prepare analyzer sidecar: {err}"))?
        .args(["--analyze-file", &file_path])
        .output()
        .await
        .map_err(|err| format!("Failed to run local analyzer: {err}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let json_line = stdout
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| line.starts_with('{'))
        .ok_or_else(|| {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            if stderr.is_empty() {
                "Analyzer returned no result.".to_string()
            } else {
                format!("Analyzer returned no result. {stderr}")
            }
        })?;

    let mut parsed: ProjectAnalysisResult = serde_json::from_str(json_line)
        .map_err(|err| format!("Failed to parse analyzer result: {err}"))?;

    if !output.status.success() && parsed.error.is_none() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if !stderr.is_empty() {
            parsed.error = Some(stderr);
        }
    }

    Ok(parsed)
}

#[tauri::command]
pub async fn upload_project_file(
    app: tauri::AppHandle,
    file_path: String,
    upload_url: String,
) -> Result<(), String> {
    let input_path = PathBuf::from(&file_path);
    if !input_path.exists() {
        return Err("Selected file no longer exists.".to_string());
    }
    validate_project_extension(&input_path)?;

    let metadata = fs::metadata(&input_path)
        .map_err(|err| format!("Failed to read project file metadata: {err}"))?;
    let total_size = metadata.len();
    if total_size == 0 {
        return Err("Project file is empty.".to_string());
    }

    let mut reader = tokio::fs::File::open(&input_path)
        .await
        .map_err(|err| format!("Failed to open project file: {err}"))?;
    let mut bytes = Vec::new();
    let mut buf = vec![0u8; 1024 * 1024];
    let mut loaded = 0u64;

    emit_upload_progress(&app, 0, total_size);
    loop {
        let n = reader
            .read(&mut buf)
            .await
            .map_err(|err| format!("Failed to read project file: {err}"))?;
        if n == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..n]);
        loaded += n as u64;

        let scaled = if total_size > 0 {
            ((loaded as f64 / total_size as f64) * 95.0).round() as u64
        } else {
            0
        };
        emit_upload_progress(&app, scaled.min(95), 100);
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(60 * 30))
        .build()
        .map_err(|err| format!("Failed to initialize upload client: {err}"))?;
    let response = client
        .put(&upload_url)
        .header("Content-Type", "application/octet-stream")
        .body(bytes)
        .send()
        .await
        .map_err(|err| format!("Upload request failed: {err}"))?;

    if !response.status().is_success() {
        let status = response.status();
        let detail = response.text().await.unwrap_or_default();
        return Err(if detail.trim().is_empty() {
            format!("Upload failed with status {status}")
        } else {
            format!("Upload failed with status {status}: {detail}")
        });
    }

    emit_upload_progress(&app, total_size, total_size);
    Ok(())
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
