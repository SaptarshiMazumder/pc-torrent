use serde_json::json;
use serde::Serialize;
use reqwest::header::CONTENT_DISPOSITION;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tauri::{AppHandle, State};
use tokio::sync::Mutex;

// Embed the prepare script at compile time so it ships inside the binary
const PREPARE_BLEND_PY: &str = include_str!("../../../runpod_worker/prepare_blend.py");

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

#[tauri::command]
pub fn get_file_size(file_path: String) -> Result<u64, String> {
    let path = Path::new(&file_path);
    let metadata = fs::metadata(path)
        .map_err(|err| format!("Failed to read file metadata: {err}"))?;
    if !metadata.is_file() {
        return Err("Selected path is not a file.".to_string());
    }
    Ok(metadata.len())
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

fn sanitize_path_component(name: &str, fallback: &str) -> String {
    let sanitized = sanitize_filename(name);
    let trimmed = sanitized.trim().trim_end_matches('.').trim();
    if trimmed.is_empty() {
        fallback.to_string()
    } else {
        trimmed.to_string()
    }
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
pub async fn clear_logs(
    app: tauri::AppHandle,
    state: State<'_, Arc<Mutex<AgentState>>>,
) -> Result<(), String> {
    let snapshot = {
        let mut s = state.lock().await;
        s.logs.clear();
        s.clone()
    };

    save_agent_state(&app, &snapshot)?;
    Ok(())
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
    force: Option<bool>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;
    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({"cmd": "run_preflight", "force": force.unwrap_or(false)}))
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
    job_folder: Option<String>,
    preferred_filename: Option<String>,
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

    let filename = preferred_filename
        .filter(|name| !name.trim().is_empty())
        .unwrap_or_else(|| {
            response
                .headers()
                .get(CONTENT_DISPOSITION)
                .and_then(|value| value.to_str().ok())
                .and_then(parse_download_filename)
                .or_else(|| filename_from_url(response.url().as_str()))
                .unwrap_or_else(|| "download.bin".to_string())
        });

    let downloads = downloads_dir()?;
    let target_dir = if let Some(folder) = job_folder {
        downloads.join(sanitize_path_component(&folder, "render_job"))
    } else {
        downloads
    };
    fs::create_dir_all(&target_dir)
        .map_err(|err| format!("Failed to create destination folder: {err}"))?;

    let file_path = unique_download_path(&target_dir, &filename);
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

// ---------------------------------------------------------------------------
// Blend file preparation
// ---------------------------------------------------------------------------

#[derive(Serialize)]
pub struct PrepareResult {
    pub prepared_path: String,
    pub filename: String,
    pub warnings: Vec<String>,
    pub errors: Vec<String>,
    pub prep_done: bool,
}

/// Search common install locations for the Blender binary.
#[tauri::command]
pub fn find_blender() -> Option<String> {
    // 1. Explicit env override
    if let Ok(bin) = std::env::var("BLENDER_BIN") {
        if Path::new(&bin).is_file() {
            return Some(bin);
        }
    }

    // 2. Common Windows install paths (newest first)
    #[cfg(target_os = "windows")]
    {
        let versions = ["5.1", "5.0", "4.4", "4.3", "4.2", "4.1", "4.0", "3.6", "3.5", "3.4", "3.3"];
        for ver in &versions {
            let p = format!(r"C:\Program Files\Blender Foundation\Blender {ver}\blender.exe");
            if Path::new(&p).is_file() {
                return Some(p);
            }
        }
        // Generic (some installers don't include version in folder name)
        let generic = r"C:\Program Files\Blender Foundation\blender.exe";
        if Path::new(generic).is_file() {
            return Some(generic.to_string());
        }
        // `where blender` via shell
        if let Ok(out) = std::process::Command::new("where").arg("blender").output() {
            if out.status.success() {
                if let Ok(s) = std::str::from_utf8(&out.stdout) {
                    for line in s.lines() {
                        let p = line.trim();
                        if !p.is_empty() && Path::new(p).is_file() {
                            return Some(p.to_string());
                        }
                    }
                }
            }
        }
    }

    // 3. macOS / Linux fallbacks
    #[cfg(not(target_os = "windows"))]
    {
        let candidates = [
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "/usr/bin/blender",
            "/usr/local/bin/blender",
        ];
        for p in &candidates {
            if Path::new(p).is_file() {
                return Some(p.to_string());
            }
        }
        if let Ok(out) = std::process::Command::new("which").arg("blender").output() {
            if out.status.success() {
                if let Ok(s) = std::str::from_utf8(&out.stdout) {
                    let p = s.trim();
                    if !p.is_empty() {
                        return Some(p.to_string());
                    }
                }
            }
        }
    }

    None
}

fn _find_blend_files(dir: &Path) -> Vec<PathBuf> {
    let mut results = Vec::new();
    if let Ok(entries) = fs::read_dir(dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                results.extend(_find_blend_files(&path));
            } else if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                let lower = name.to_lowercase();
                if lower.ends_with(".blend") && !lower.starts_with("._") {
                    results.push(path);
                }
            }
        }
    }
    results.sort();
    results
}

fn _choose_target_blend(source_stem: &str, root: &Path, blends: &[PathBuf]) -> PathBuf {
    // Prefer root-level files; within that, prefer stem match, then largest file
    let mut best: Option<(&PathBuf, usize, bool, u64)> = None; // (path, depth, stem_match, size)
    for p in blends {
        let rel = p.strip_prefix(root).unwrap_or(p);
        let depth = rel.components().count().saturating_sub(1);
        let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_lowercase();
        let stem_match = stem == source_stem.to_lowercase();
        let size = fs::metadata(p).map(|m| m.len()).unwrap_or(0);
        let better = match &best {
            None => true,
            Some((_, bd, bm, bs)) => {
                let b_is_root = *bd == 0;
                let c_is_root = depth == 0;
                if b_is_root != c_is_root {
                    c_is_root
                } else if *bm != stem_match {
                    stem_match
                } else {
                    size > *bs
                }
            }
        };
        if better {
            best = Some((p, depth, stem_match, size));
        }
    }
    best.map(|(p, ..)| p.clone()).unwrap_or_else(|| blends[0].clone())
}

fn _zip_dir(src: &Path, dest: &Path) -> Result<(), String> {
    let file = File::create(dest).map_err(|e| format!("Cannot create zip: {e}"))?;
    let mut zip = zip::ZipWriter::new(file);
    let opts = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);
    _zip_dir_recursive(&mut zip, src, src, opts)?;
    zip.finish().map_err(|e| format!("Zip finish failed: {e}"))?;
    Ok(())
}

fn _zip_dir_recursive(
    zip: &mut zip::ZipWriter<File>,
    dir: &Path,
    base: &Path,
    opts: zip::write::SimpleFileOptions,
) -> Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|e| e.to_string())?.flatten() {
        let path = entry.path();
        let rel = path
            .strip_prefix(base)
            .map_err(|e| e.to_string())?
            .to_string_lossy()
            .replace('\\', "/");
        if path.is_dir() {
            zip.add_directory(&rel, opts).map_err(|e| e.to_string())?;
            _zip_dir_recursive(zip, &path, base, opts)?;
        } else {
            zip.start_file(&rel, opts).map_err(|e| e.to_string())?;
            let mut f = File::open(&path).map_err(|e| e.to_string())?;
            let mut buf = Vec::new();
            f.read_to_end(&mut buf).map_err(|e| e.to_string())?;
            Write::write_all(zip, &buf).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Run prepare_blend.py inside Blender on the selected file, pack all assets,
/// and return the path to the prepared file (ready for upload).
#[tauri::command]
pub async fn prepare_blend_for_upload(
    file_path: String,
    blender_bin: String,
) -> Result<PrepareResult, String> {
    let source = Path::new(&file_path);
    let filename = source
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or("Invalid file path")?
        .to_string();

    // Unique work directory inside %TEMP%/pcrent_prep/
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let work_dir = std::env::temp_dir()
        .join("pcrent_prep")
        .join(format!("job_{ts}"));
    fs::create_dir_all(&work_dir).map_err(|e| format!("Cannot create work dir: {e}"))?;

    // Write prepare script
    let prep_script = work_dir.join("prepare_blend.py");
    fs::write(&prep_script, PREPARE_BLEND_PY)
        .map_err(|e| format!("Cannot write prepare script: {e}"))?;

    let is_zip = filename.to_lowercase().ends_with(".zip");

    let blend_path: PathBuf;
    let extract_dir: Option<PathBuf>;

    if is_zip {
        let zip_bytes = fs::read(source).map_err(|e| format!("Cannot read zip: {e}"))?;
        let cursor = std::io::Cursor::new(zip_bytes);
        let mut archive = zip::ZipArchive::new(cursor)
            .map_err(|e| format!("Invalid zip archive: {e}"))?;
        let ex = work_dir.join("extracted");
        fs::create_dir_all(&ex).map_err(|e| e.to_string())?;
        archive
            .extract(&ex)
            .map_err(|e| format!("Zip extract failed: {e}"))?;
        let blends = _find_blend_files(&ex);
        if blends.is_empty() {
            return Err("No .blend file found inside zip".to_string());
        }
        let source_stem = Path::new(&filename)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        blend_path = _choose_target_blend(source_stem, &ex, &blends);
        extract_dir = Some(ex);
    } else {
        // Copy blend to work dir so Blender writes the prepared file there
        let dest = work_dir.join(&filename);
        fs::copy(source, &dest).map_err(|e| format!("Cannot copy blend: {e}"))?;
        blend_path = dest;
        extract_dir = None;
    }

    // Run Blender headless with prepare script
    let output = std::process::Command::new(&blender_bin)
        .arg("-b")
        .arg(&blend_path)
        .arg("--python")
        .arg(&prep_script)
        .output()
        .map_err(|e| format!("Failed to launch Blender: {e}"))?;

    let combined = format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let mut warnings = Vec::new();
    let mut errors = Vec::new();
    let mut prep_done = false;

    for line in combined.lines() {
        if let Some(msg) = line.strip_prefix("PREP_WARN:") {
            warnings.push(msg.trim().to_string());
        } else if let Some(msg) = line.strip_prefix("PREP_ERROR:") {
            errors.push(msg.trim().to_string());
        } else if line.trim() == "PREP_DONE" {
            prep_done = true;
        }
    }

    // Package the result
    let (prepared_path, prepared_filename) = if is_zip {
        // Re-zip the extracted (now prepared) directory
        let new_zip = work_dir.join(&filename);
        let ex = extract_dir.unwrap();
        _zip_dir(&ex, &new_zip)?;
        (new_zip.to_string_lossy().to_string(), filename)
    } else {
        (blend_path.to_string_lossy().to_string(), filename)
    };

    Ok(PrepareResult {
        prepared_path,
        filename: prepared_filename,
        warnings,
        errors,
        prep_done,
    })
}
