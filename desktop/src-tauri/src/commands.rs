use serde_json::json;
use serde::{Deserialize, Serialize};
use reqwest::header::{AUTHORIZATION, CONTENT_DISPOSITION, CONTENT_TYPE};
use futures_util::StreamExt;
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use std::collections::HashMap;
use std::error::Error;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, State};
use tokio::io::{AsyncReadExt, AsyncSeekExt, SeekFrom};
use tokio::sync::Mutex;
use tokio_util::io::ReaderStream;

// Embed the prepare script at compile time so it ships inside the binary
const PREPARE_BLEND_PY: &str = include_str!("../../../runpod_worker/prepare_blend.py");
const ANALYZE_BLEND_PY: &str = r#"
import json
import bpy

def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default

def _minimal_scene_payload(scene, active_name):
    frame_start = _safe_int(getattr(scene, "frame_start", 1), 1)
    frame_end = _safe_int(getattr(scene, "frame_end", frame_start), frame_start)
    frame_step = max(1, _safe_int(getattr(scene, "frame_step", 1), 1))
    total_frames = ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    scene_name = getattr(scene, "name", "Scene")
    camera = getattr(scene, "camera", None)
    active_camera = getattr(camera, "name", None) if camera else None
    return {
        "name": scene_name,
        "is_active": scene_name == active_name,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "total_frames": total_frames,
        "active_camera": active_camera,
        "cameras": [active_camera] if active_camera else [],
        "view_layers": [],
        "camera_cuts": [],
    }

def _scene_payload(scene, active_name):
    payload = _minimal_scene_payload(scene, active_name)

    cameras = list(payload["cameras"])
    camera_cuts = []

    try:
        markers = sorted(getattr(scene, "timeline_markers", []), key=lambda marker: _safe_int(getattr(marker, "frame", 0), 0))
        for marker in markers:
            marker_camera = None
            try:
                marker_camera = marker.camera.name if getattr(marker, "camera", None) else None
            except Exception:
                marker_camera = None
            if marker_camera:
                cameras.append(marker_camera)
            camera_cuts.append({
                "frame": _safe_int(getattr(marker, "frame", 0), 0),
                "camera_name": marker_camera,
            })
    except Exception:
        pass

    try:
        for obj in bpy.data.objects:
            if getattr(obj, "type", "") == "CAMERA":
                cameras.append(getattr(obj, "name", "Camera"))
    except Exception:
        pass

    unique_cameras = []
    for name in cameras:
        if name and name not in unique_cameras:
            unique_cameras.append(name)

    view_layers = []
    try:
        view_layers = [getattr(layer, "name", "") for layer in getattr(scene, "view_layers", []) if getattr(layer, "name", "")]
    except Exception:
        view_layers = []

    payload["cameras"] = unique_cameras
    payload["view_layers"] = view_layers
    payload["camera_cuts"] = camera_cuts
    return payload

active_scene = bpy.context.scene
active_name = active_scene.name if active_scene else (bpy.data.scenes[0].name if bpy.data.scenes else "")
scenes = []
for scene in bpy.data.scenes:
    try:
        scenes.append(_scene_payload(scene, active_name))
    except Exception:
        scenes.append(_minimal_scene_payload(scene, active_name))
if not scenes:
    raise RuntimeError("No scenes found in file")

active = None
for scene in scenes:
    if scene.get("is_active"):
        active = scene
        break
if active is None:
    active = scenes[0]

v = bpy.app.version
blender_version = int(v[0]) * 100 + int(v[1])

payload = {
    "frame_start": active["frame_start"],
    "frame_end": active["frame_end"],
    "frame_step": active["frame_step"],
    "total_frames": active["total_frames"],
    "blender_version": blender_version,
    "active_scene": active["name"],
    "cameras": active.get("cameras", []),
    "camera_cuts": active.get("camera_cuts", []),
    "view_layers": active.get("view_layers", []),
    "timeline_defaults": {
        "frame_start": active["frame_start"],
        "frame_end": active["frame_end"],
        "frame_step": active["frame_step"],
    },
    "output_defaults": None,
    "render_defaults": {},
    "scenes": scenes,
    "unsupported_fields": [],
}

print("PCR_ANALYSIS_JSON:" + json.dumps(payload, separators=(",", ":")))
"#;

use crate::persistence::save_agent_state;
use crate::sidecar::{SidecarHandle, spawn_sidecar};
use crate::state::{AgentState, LogEntry};

#[derive(Serialize)]
pub struct DownloadResult {
    pub path: String,
    pub filename: String,
    pub action: String,
}

#[derive(Clone, Copy)]
enum UploadTaskStatus {
    Running,
    Completed,
    Failed,
    Cancelled,
}

impl UploadTaskStatus {
    fn as_str(self) -> &'static str {
        match self {
            UploadTaskStatus::Running => "running",
            UploadTaskStatus::Completed => "completed",
            UploadTaskStatus::Failed => "failed",
            UploadTaskStatus::Cancelled => "cancelled",
        }
    }
}

#[derive(Clone)]
struct UploadTaskEntry {
    uploaded_bytes: u64,
    total_bytes: u64,
    status: UploadTaskStatus,
    error: Option<String>,
    cancel_requested: bool,
}

#[derive(Serialize)]
pub struct UploadProgressSnapshot {
    pub status: String,
    pub progress_pct: u8,
    pub uploaded_bytes: u64,
    pub total_bytes: u64,
    pub error: Option<String>,
}

#[derive(Deserialize)]
struct MultipartInitResponse {
    upload_id: String,
    part_size_bytes: u64,
    total_parts: u32,
}

#[derive(Deserialize)]
struct MultipartPartUrlsResponse {
    urls: HashMap<String, String>,
}

static UPLOAD_TASKS: OnceLock<StdMutex<HashMap<String, UploadTaskEntry>>> = OnceLock::new();
static NEXT_UPLOAD_ID: AtomicU64 = AtomicU64::new(1);
const MAX_UPLOAD_BYTES: u64 = 100 * 1024 * 1024 * 1024; // 100 GB
const MULTIPART_MIN_PART_SIZE_BYTES: u64 = 5 * 1024 * 1024;
const PART_URL_BATCH_SIZE: u32 = 16;

fn upload_tasks() -> &'static StdMutex<HashMap<String, UploadTaskEntry>> {
    UPLOAD_TASKS.get_or_init(|| StdMutex::new(HashMap::new()))
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

#[tauri::command]
pub fn read_file_head_base64(file_path: String, max_bytes: u64) -> Result<String, String> {
    let path = Path::new(&file_path);
    let metadata = fs::metadata(path)
        .map_err(|err| format!("Failed to read file metadata: {err}"))?;
    if !metadata.is_file() {
        return Err("Selected path is not a file.".to_string());
    }

    let file_size = metadata.len();
    if file_size == 0 {
        return Ok(String::new());
    }

    let requested = if max_bytes == 0 { 1 } else { max_bytes };
    let to_read_u64 = file_size.min(requested);
    let to_read: usize = to_read_u64
        .try_into()
        .map_err(|_| "Requested read size is too large for this platform".to_string())?;

    let mut file = File::open(path)
        .map_err(|err| format!("Failed to open file: {err}"))?;
    let mut buffer = vec![0_u8; to_read];
    let mut offset = 0_usize;
    while offset < buffer.len() {
        let n = file
            .read(&mut buffer[offset..])
            .map_err(|err| format!("Failed to read file: {err}"))?;
        if n == 0 {
            break;
        }
        offset += n;
    }
    buffer.truncate(offset);

    Ok(BASE64.encode(&buffer))
}

#[tauri::command]
pub async fn start_upload_file_to_presigned_url(
    file_path: String,
    upload_url: String,
) -> Result<String, String> {
    let path = PathBuf::from(&file_path);
    let metadata = tokio::fs::metadata(&path)
        .await
        .map_err(|err| format!("Failed to read file metadata: {err}"))?;
    if !metadata.is_file() {
        return Err("Selected path is not a file.".to_string());
    }

    let total_bytes = metadata.len();
    if total_bytes > MAX_UPLOAD_BYTES {
        return Err(format!(
            "File exceeds max upload size (100 GB). Size: {:.2} GB",
            (total_bytes as f64) / (1024.0 * 1024.0 * 1024.0)
        ));
    }
    let upload_id = format!(
        "upload-{}",
        NEXT_UPLOAD_ID.fetch_add(1, Ordering::Relaxed)
    );

    {
        let mut tasks = upload_tasks()
            .lock()
            .map_err(|_| "Failed to acquire upload task lock".to_string())?;
        tasks.insert(
            upload_id.clone(),
            UploadTaskEntry {
                uploaded_bytes: 0,
                total_bytes,
                status: UploadTaskStatus::Running,
                error: None,
                cancel_requested: false,
            },
        );
    }

    let upload_id_for_task = upload_id.clone();
    tokio::spawn(async move {
        let outcome = upload_file_streaming(path, upload_url, upload_id_for_task.clone(), total_bytes).await;
        if let Ok(mut tasks) = upload_tasks().lock() {
            if let Some(task) = tasks.get_mut(&upload_id_for_task) {
                let cancelled = task.cancel_requested || matches!(task.status, UploadTaskStatus::Cancelled);
                match outcome {
                    Ok(()) => {
                        if cancelled {
                            task.status = UploadTaskStatus::Cancelled;
                            if task.error.is_none() {
                                task.error = Some("Cancelled by user".to_string());
                            }
                        } else {
                            task.uploaded_bytes = task.total_bytes;
                            task.status = UploadTaskStatus::Completed;
                            task.error = None;
                        }
                    }
                    Err(err) => {
                        if cancelled || err.to_lowercase().contains("cancelled") {
                            task.status = UploadTaskStatus::Cancelled;
                            task.error = Some("Cancelled by user".to_string());
                        } else {
                            task.status = UploadTaskStatus::Failed;
                            task.error = Some(err);
                        }
                    }
                }
            }
        }
    });

    Ok(upload_id)
}

fn normalize_backend_base_url(raw: &str) -> String {
    raw.trim().trim_end_matches('/').to_string()
}

fn upload_cancelled(upload_id: &str) -> bool {
    if let Ok(tasks) = upload_tasks().lock() {
        if let Some(task) = tasks.get(upload_id) {
            return task.cancel_requested || matches!(task.status, UploadTaskStatus::Cancelled);
        }
    }
    false
}

fn set_uploaded_bytes(upload_id: &str, uploaded_bytes: u64) {
    if let Ok(mut tasks) = upload_tasks().lock() {
        if let Some(task) = tasks.get_mut(upload_id) {
            task.uploaded_bytes = uploaded_bytes.min(task.total_bytes);
        }
    }
}

fn current_uploaded_bytes(upload_id: &str) -> u64 {
    if let Ok(tasks) = upload_tasks().lock() {
        if let Some(task) = tasks.get(upload_id) {
            return task.uploaded_bytes;
        }
    }
    0
}

async fn response_error_detail(response: reqwest::Response) -> String {
    let status = response.status();
    let body = response.text().await.unwrap_or_default();
    if body.trim().is_empty() {
        format!("HTTP {status}")
    } else {
        format!("HTTP {status}: {body}")
    }
}

async fn post_json(
    client: &reqwest::Client,
    url: &str,
    body: serde_json::Value,
    auth_token: Option<&str>,
) -> Result<reqwest::Response, String> {
    let mut req = client
        .post(url)
        .header(CONTENT_TYPE, "application/json");
    if let Some(token) = auth_token.map(str::trim).filter(|t| !t.is_empty()) {
        req = req.header(AUTHORIZATION, format!("Bearer {token}"));
    }
    req.body(body.to_string())
        .send()
        .await
        .map_err(|err| format!("Request failed ({url}): {err}"))
}

async fn abort_render_group_multipart_best_effort(
    client: &reqwest::Client,
    base_url: &str,
    group_id: &str,
    remote_upload_id: &str,
    auth_token: Option<&str>,
) {
    let abort_url = format!("{base_url}/render-groups/{group_id}/multipart-upload/abort");
    let _ = post_json(
        client,
        &abort_url,
        json!({
            "upload_id": remote_upload_id
        }),
        auth_token,
    )
    .await;
}

#[tauri::command]
pub async fn start_upload_file_to_render_group_multipart(
    file_path: String,
    backend_url: String,
    group_id: String,
    auth_token: Option<String>,
) -> Result<String, String> {
    let path = PathBuf::from(&file_path);
    let metadata = tokio::fs::metadata(&path)
        .await
        .map_err(|err| format!("Failed to read file metadata: {err}"))?;
    if !metadata.is_file() {
        return Err("Selected path is not a file.".to_string());
    }

    let total_bytes = metadata.len();
    if total_bytes > MAX_UPLOAD_BYTES {
        return Err(format!(
            "File exceeds max upload size (100 GB). Size: {:.2} GB",
            (total_bytes as f64) / (1024.0 * 1024.0 * 1024.0)
        ));
    }

    let upload_id = format!(
        "upload-{}",
        NEXT_UPLOAD_ID.fetch_add(1, Ordering::Relaxed)
    );
    {
        let mut tasks = upload_tasks()
            .lock()
            .map_err(|_| "Failed to acquire upload task lock".to_string())?;
        tasks.insert(
            upload_id.clone(),
            UploadTaskEntry {
                uploaded_bytes: 0,
                total_bytes,
                status: UploadTaskStatus::Running,
                error: None,
                cancel_requested: false,
            },
        );
    }

    let upload_id_for_task = upload_id.clone();
    let backend_url_for_task = normalize_backend_base_url(&backend_url);
    let group_id_for_task = group_id.clone();
    let auth_token_for_task = auth_token.clone();

    tokio::spawn(async move {
        let outcome = upload_file_to_render_group_multipart(
            path,
            backend_url_for_task,
            group_id_for_task,
            upload_id_for_task.clone(),
            total_bytes,
            auth_token_for_task,
        )
        .await;

        if let Ok(mut tasks) = upload_tasks().lock() {
            if let Some(task) = tasks.get_mut(&upload_id_for_task) {
                let cancelled = task.cancel_requested || matches!(task.status, UploadTaskStatus::Cancelled);
                match outcome {
                    Ok(()) => {
                        if cancelled {
                            task.status = UploadTaskStatus::Cancelled;
                            if task.error.is_none() {
                                task.error = Some("Cancelled by user".to_string());
                            }
                        } else {
                            task.uploaded_bytes = task.total_bytes;
                            task.status = UploadTaskStatus::Completed;
                            task.error = None;
                        }
                    }
                    Err(err) => {
                        if cancelled || err.to_lowercase().contains("cancelled") {
                            task.status = UploadTaskStatus::Cancelled;
                            task.error = Some("Cancelled by user".to_string());
                        } else {
                            task.status = UploadTaskStatus::Failed;
                            task.error = Some(err);
                        }
                    }
                }
            }
        }
    });

    Ok(upload_id)
}

async fn upload_file_to_render_group_multipart(
    file_path: PathBuf,
    backend_url: String,
    group_id: String,
    upload_id: String,
    total_bytes: u64,
    auth_token: Option<String>,
) -> Result<(), String> {
    let client = reqwest::Client::new();
    let init_url = format!("{backend_url}/render-groups/{group_id}/multipart-upload/init");
    let init_response = post_json(
        &client,
        &init_url,
        json!({
            "file_size_bytes": total_bytes,
            "content_type": "application/octet-stream",
        }),
        auth_token.as_deref(),
    )
    .await?;
    if !init_response.status().is_success() {
        return Err(format!(
            "Failed to init multipart upload: {}",
            response_error_detail(init_response).await
        ));
    }

    let init_text = init_response
        .text()
        .await
        .map_err(|err| format!("Failed reading multipart init response: {err}"))?;
    let init: MultipartInitResponse = serde_json::from_str(&init_text)
        .map_err(|err| format!("Invalid multipart init response: {err}"))?;

    let part_size_bytes = init
        .part_size_bytes
        .max(MULTIPART_MIN_PART_SIZE_BYTES);
    let total_parts = init.total_parts.max(1);
    let remote_upload_id = init.upload_id;
    let part_urls_endpoint = format!("{backend_url}/render-groups/{group_id}/multipart-upload/part-urls");
    let complete_endpoint = format!("{backend_url}/render-groups/{group_id}/multipart-upload/complete");

    let mut completed_parts: Vec<serde_json::Value> = Vec::with_capacity(total_parts as usize);
    let mut url_cache: HashMap<u32, String> = HashMap::new();

    for part_number in 1..=total_parts {
        if upload_cancelled(&upload_id) {
            abort_render_group_multipart_best_effort(
                &client,
                &backend_url,
                &group_id,
                &remote_upload_id,
                auth_token.as_deref(),
            )
            .await;
            return Err("Upload cancelled by user".to_string());
        }

        if !url_cache.contains_key(&part_number) {
            let mut batch: Vec<u32> = Vec::new();
            let batch_end = (part_number + PART_URL_BATCH_SIZE - 1).min(total_parts);
            for n in part_number..=batch_end {
                batch.push(n);
            }

            let urls_response = post_json(
                &client,
                &part_urls_endpoint,
                json!({
                    "upload_id": remote_upload_id,
                    "part_numbers": batch,
                }),
                auth_token.as_deref(),
            )
            .await?;
            if !urls_response.status().is_success() {
                abort_render_group_multipart_best_effort(
                    &client,
                    &backend_url,
                    &group_id,
                    &remote_upload_id,
                    auth_token.as_deref(),
                )
                .await;
                return Err(format!(
                    "Failed to fetch multipart part URLs: {}",
                    response_error_detail(urls_response).await
                ));
            }
            let urls_text = urls_response
                .text()
                .await
                .map_err(|err| format!("Failed reading multipart part-url response: {err}"))?;
            let parsed: MultipartPartUrlsResponse = serde_json::from_str(&urls_text)
                .map_err(|err| format!("Invalid multipart part-url response: {err}"))?;
            for (k, v) in parsed.urls {
                if let Ok(n) = k.parse::<u32>() {
                    url_cache.insert(n, v);
                }
            }
        }

        let part_url = url_cache
            .remove(&part_number)
            .ok_or_else(|| format!("Missing upload URL for part {part_number}"))?;

        let offset = ((part_number as u64) - 1) * part_size_bytes;
        let remaining = total_bytes.saturating_sub(offset);
        let this_part_size = remaining.min(part_size_bytes);
        if this_part_size == 0 {
            break;
        }

        let mut part_uploaded = false;
        let mut last_err: Option<String> = None;
        let committed_before_part = current_uploaded_bytes(&upload_id);
        for attempt in 1..=3 {
            if upload_cancelled(&upload_id) {
                abort_render_group_multipart_best_effort(
                    &client,
                    &backend_url,
                    &group_id,
                    &remote_upload_id,
                    auth_token.as_deref(),
                )
                .await;
                return Err("Upload cancelled by user".to_string());
            }

            let mut stream_file = tokio::fs::File::open(&file_path)
                .await
                .map_err(|err| format!("Failed to open file for part {part_number}: {err}"))?;
            stream_file
                .seek(SeekFrom::Start(offset))
                .await
                .map_err(|err| format!("Failed to seek file for part {part_number}: {err}"))?;
            let limited_reader = stream_file.take(this_part_size);
            let upload_id_for_stream = upload_id.clone();
            let mut sent_in_attempt: u64 = 0;
            let stream = ReaderStream::with_capacity(limited_reader, 256 * 1024).map(move |item| {
                if upload_cancelled(&upload_id_for_stream) {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::Interrupted,
                        "Upload cancelled by user",
                    ));
                }
                item.map(|chunk| {
                    sent_in_attempt = sent_in_attempt.saturating_add(chunk.len() as u64);
                    set_uploaded_bytes(
                        &upload_id_for_stream,
                        committed_before_part.saturating_add(sent_in_attempt),
                    );
                    chunk
                })
            });

            let part_response = client
                .put(&part_url)
                .header(CONTENT_TYPE, "application/octet-stream")
                .header(reqwest::header::CONTENT_LENGTH, this_part_size.to_string())
                .body(reqwest::Body::wrap_stream(stream))
                .send()
                .await;

            match part_response {
                Ok(response) => {
                    if response.status().is_success() {
                        let etag = response
                            .headers()
                            .get("etag")
                            .and_then(|v| v.to_str().ok())
                            .map(|s| s.to_string())
                            .or_else(|| {
                                response
                                    .headers()
                                    .get("ETag")
                                    .and_then(|v| v.to_str().ok())
                                    .map(|s| s.to_string())
                            })
                            .ok_or_else(|| format!("Missing ETag for part {part_number}"))?;

                        completed_parts.push(json!({
                            "part_number": part_number,
                            "etag": etag,
                        }));
                        set_uploaded_bytes(
                            &upload_id,
                            committed_before_part.saturating_add(this_part_size),
                        );
                        part_uploaded = true;
                        break;
                    }
                    set_uploaded_bytes(&upload_id, committed_before_part);
                    last_err = Some(format!(
                        "Part {part_number} upload failed (attempt {attempt}/3): {}",
                        response_error_detail(response).await
                    ));
                }
                Err(err) => {
                    set_uploaded_bytes(&upload_id, committed_before_part);
                    last_err = Some(format!(
                        "Part {part_number} request failed (attempt {attempt}/3): {err}"
                    ));
                }
            }

            tokio::time::sleep(std::time::Duration::from_millis(350 * attempt as u64)).await;
        }

        if !part_uploaded {
            abort_render_group_multipart_best_effort(
                &client,
                &backend_url,
                &group_id,
                &remote_upload_id,
                auth_token.as_deref(),
            )
            .await;
            return Err(last_err.unwrap_or_else(|| format!("Failed uploading part {part_number}")));
        }
    }

    let complete_response = post_json(
        &client,
        &complete_endpoint,
        json!({
            "upload_id": remote_upload_id,
            "parts": completed_parts,
        }),
        auth_token.as_deref(),
    )
    .await?;
    if !complete_response.status().is_success() {
        abort_render_group_multipart_best_effort(
            &client,
            &backend_url,
            &group_id,
            &remote_upload_id,
            auth_token.as_deref(),
        )
        .await;
        return Err(format!(
            "Failed to complete multipart upload: {}",
            response_error_detail(complete_response).await
        ));
    }

    Ok(())
}

async fn upload_file_streaming(
    file_path: PathBuf,
    upload_url: String,
    upload_id: String,
    total_bytes: u64,
) -> Result<(), String> {
    let file = tokio::fs::File::open(&file_path)
        .await
        .map_err(|err| format!("Failed to open file for upload: {err}"))?;

    let upload_id_for_stream = upload_id.clone();
    let stream = ReaderStream::with_capacity(file, 256 * 1024).map(move |item| {
        if upload_cancelled(&upload_id_for_stream) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Interrupted,
                "Upload cancelled by user",
            ));
        }
        item.map(|chunk| {
            if let Ok(mut tasks) = upload_tasks().lock() {
                if let Some(task) = tasks.get_mut(&upload_id_for_stream) {
                    task.uploaded_bytes = task
                        .uploaded_bytes
                        .saturating_add(chunk.len() as u64)
                        .min(task.total_bytes);
                }
            }
            chunk
        })
    });

    let response = reqwest::Client::new()
        .put(&upload_url)
        .header(CONTENT_TYPE, "application/octet-stream")
        .header(reqwest::header::CONTENT_LENGTH, total_bytes.to_string())
        .body(reqwest::Body::wrap_stream(stream))
        .send()
        .await
        .map_err(|err| {
            if upload_cancelled(&upload_id) {
                return "Upload cancelled by user".to_string();
            }
            let mut msg = err.to_string();
            let mut source = err.source();
            while let Some(src) = source {
                msg.push_str(": ");
                msg.push_str(&src.to_string());
                source = src.source();
            }
            format!("Upload request failed: {msg}")
        })?;

    if !response.status().is_success() {
        let status = response.status();
        let detail = response.text().await.unwrap_or_default();
        return Err(if detail.trim().is_empty() {
            format!("Upload failed with status {status}")
        } else {
            format!("Upload failed with status {status}: {detail}")
        });
    }

    Ok(())
}

#[tauri::command]
pub fn get_upload_progress(upload_id: String) -> Result<UploadProgressSnapshot, String> {
    let tasks = upload_tasks()
        .lock()
        .map_err(|_| "Failed to acquire upload task lock".to_string())?;
    let task = tasks
        .get(&upload_id)
        .ok_or_else(|| "Upload not found".to_string())?;

    let raw_progress = if task.total_bytes == 0 {
        0
    } else {
        ((task.uploaded_bytes.saturating_mul(100)) / task.total_bytes)
            .min(100) as u8
    };
    let progress_pct = match task.status {
        UploadTaskStatus::Completed => 100,
        UploadTaskStatus::Running => raw_progress.min(99),
        UploadTaskStatus::Failed => raw_progress.min(99),
        UploadTaskStatus::Cancelled => raw_progress.min(99),
    };

    Ok(UploadProgressSnapshot {
        status: task.status.as_str().to_string(),
        progress_pct,
        uploaded_bytes: task.uploaded_bytes,
        total_bytes: task.total_bytes,
        error: task.error.clone(),
    })
}

#[tauri::command]
pub fn cancel_upload_progress(upload_id: String) -> Result<(), String> {
    let mut tasks = upload_tasks()
        .lock()
        .map_err(|_| "Failed to acquire upload task lock".to_string())?;
    let task = tasks
        .get_mut(&upload_id)
        .ok_or_else(|| "Upload not found".to_string())?;

    task.cancel_requested = true;
    task.status = UploadTaskStatus::Cancelled;
    if task.error.is_none() {
        task.error = Some("Cancelled by user".to_string());
    }
    Ok(())
}

#[tauri::command]
pub fn clear_upload_progress(upload_id: String) -> Result<(), String> {
    let mut tasks = upload_tasks()
        .lock()
        .map_err(|_| "Failed to acquire upload task lock".to_string())?;
    tasks.remove(&upload_id);
    Ok(())
}

#[tauri::command]
pub async fn upload_file_to_presigned_url(
    file_path: String,
    upload_url: String,
) -> Result<(), String> {
    let upload_id = start_upload_file_to_presigned_url(file_path, upload_url).await?;
    loop {
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        let snapshot = get_upload_progress(upload_id.clone())?;
        match snapshot.status.as_str() {
            "completed" => {
                let _ = clear_upload_progress(upload_id);
                return Ok(());
            }
            "cancelled" => {
                let _ = clear_upload_progress(upload_id);
                return Err(snapshot.error.unwrap_or_else(|| "Upload cancelled".to_string()));
            }
            "failed" => {
                let _ = clear_upload_progress(upload_id);
                return Err(snapshot.error.unwrap_or_else(|| "Upload failed".to_string()));
            }
            _ => {}
        }
    }
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

fn duplicate_variant_name(stem: &str, ext: &str, candidate: &str) -> bool {
    if candidate.is_empty() || stem.is_empty() {
        return false;
    }
    let prefix = format!("{stem} (");
    if !candidate.starts_with(&prefix) {
        return false;
    }

    let suffix = if ext.is_empty() {
        ")".to_string()
    } else {
        format!("){ext}")
    };
    if !candidate.ends_with(&suffix) {
        return false;
    }

    let middle_start = prefix.len();
    let middle_end = candidate.len().saturating_sub(suffix.len());
    if middle_end <= middle_start {
        return false;
    }
    candidate[middle_start..middle_end]
        .chars()
        .all(|ch| ch.is_ascii_digit())
}

fn remove_duplicate_variants(dir: &Path, canonical_filename: &str) -> Result<(), String> {
    let canonical = Path::new(canonical_filename);
    let stem = canonical
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("download");
    let ext = canonical
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| format!(".{value}"))
        .unwrap_or_default();

    let entries = fs::read_dir(dir)
        .map_err(|err| format!("Failed to list destination folder: {err}"))?;

    for entry in entries {
        let entry = entry.map_err(|err| format!("Failed to inspect destination folder: {err}"))?;
        let path = entry.path();
        let meta = entry
            .metadata()
            .map_err(|err| format!("Failed to read destination metadata: {err}"))?;
        if !meta.is_file() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        if duplicate_variant_name(stem, &ext, name) {
            fs::remove_file(&path)
                .map_err(|err| format!("Failed to remove duplicate file '{}': {err}", path.to_string_lossy()))?;
        }
    }

    Ok(())
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
    firebase_token: Option<String>,
    state: State<'_, Arc<Mutex<AgentState>>>,
    sidecar: State<'_, Arc<Mutex<SidecarHandle>>>,
) -> Result<(), String> {
    ensure_sidecar_running(&app, &state, &sidecar).await?;

    let mut handle = sidecar.lock().await;
    handle.send_command(&json!({
        "cmd": "connect",
        "backend_url": backend_url,
        "firebase_token": firebase_token.unwrap_or_default()
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
    expected_size_bytes: Option<u64>,
    overwrite_existing: Option<bool>,
) -> Result<DownloadResult, String> {
    let preferred_name = preferred_filename
        .as_ref()
        .map(|name| name.trim())
        .filter(|name| !name.is_empty())
        .map(ToString::to_string);
    let fallback_filename = preferred_name
        .clone()
        .unwrap_or_else(|| {
            filename_from_url(&url)
                .unwrap_or_else(|| "download.bin".to_string())
        });
    let mut filename = sanitize_filename(&fallback_filename);

    let downloads = downloads_dir()?;
    let target_dir = if let Some(folder) = job_folder {
        downloads.join(sanitize_path_component(&folder, "render_job"))
    } else {
        downloads
    };
    fs::create_dir_all(&target_dir)
        .map_err(|err| format!("Failed to create destination folder: {err}"))?;
    remove_duplicate_variants(&target_dir, &filename)?;

    let mut file_path = target_dir.join(&filename);
    let should_overwrite = overwrite_existing.unwrap_or(true);

    if file_path.exists() {
        let existing_size = fs::metadata(&file_path)
            .map_err(|err| format!("Failed to read existing file metadata: {err}"))?
            .len();
        if expected_size_bytes.is_some() && expected_size_bytes == Some(existing_size) {
            return Ok(DownloadResult {
                path: file_path.to_string_lossy().to_string(),
                filename,
                action: "skipped".to_string(),
            });
        }
        if !should_overwrite {
            return Ok(DownloadResult {
                path: file_path.to_string_lossy().to_string(),
                filename,
                action: "skipped".to_string(),
            });
        }
    }

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

    if preferred_name.is_none() {
        if let Some(parsed) = response
            .headers()
            .get(CONTENT_DISPOSITION)
            .and_then(|value| value.to_str().ok())
            .and_then(parse_download_filename)
        {
            filename = sanitize_filename(&parsed);
            remove_duplicate_variants(&target_dir, &filename)?;
            file_path = target_dir.join(&filename);
            if file_path.exists() {
                let existing_size = fs::metadata(&file_path)
                    .map_err(|err| format!("Failed to read existing file metadata: {err}"))?
                    .len();
                if expected_size_bytes.is_some() && expected_size_bytes == Some(existing_size) {
                    return Ok(DownloadResult {
                        path: file_path.to_string_lossy().to_string(),
                        filename,
                        action: "skipped".to_string(),
                    });
                }
                if !should_overwrite {
                    return Ok(DownloadResult {
                        path: file_path.to_string_lossy().to_string(),
                        filename,
                        action: "skipped".to_string(),
                    });
                }
            }
        }
    }

    let action = if file_path.exists() {
        "overwritten"
    } else {
        "downloaded"
    };

    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let temp_path = target_dir.join(format!(".{}.{}.part", filename, nonce));
    let mut file = File::create(&temp_path)
        .map_err(|err| format!("Failed to create download file: {err}"))?;

    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|err| format!("Failed while downloading file: {err}"))?
    {
        file.write_all(&chunk)
            .map_err(|err| format!("Failed to write download file: {err}"))?;
    }
    file.flush()
        .map_err(|err| format!("Failed to finalize download file: {err}"))?;
    drop(file);

    if file_path.exists() {
        fs::remove_file(&file_path)
            .map_err(|err| format!("Failed to replace existing file: {err}"))?;
    }
    fs::rename(&temp_path, &file_path)
        .map_err(|err| format!("Failed to move downloaded file into place: {err}"))?;
    remove_duplicate_variants(&target_dir, &filename)?;

    Ok(DownloadResult {
        path: file_path.to_string_lossy().to_string(),
        filename,
        action: action.to_string(),
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

#[derive(Serialize)]
pub struct AnalyzeAndPrepareResult {
    pub analysis: Option<serde_json::Value>,
    pub prepared_path: Option<String>,
    pub filename: String,
    pub analysis_warnings: Vec<String>,
    pub analysis_errors: Vec<String>,
    pub prepare_warnings: Vec<String>,
    pub prepare_errors: Vec<String>,
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

fn _extract_zip_to_dir(zip_path: &Path, dest: &Path) -> Result<(), String> {
    let file = File::open(zip_path).map_err(|e| format!("Cannot open zip: {e}"))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("Invalid zip archive: {e}"))?;
    archive
        .extract(dest)
        .map_err(|e| format!("Zip extract failed: {e}"))
}

fn _tail_lines(text: &str, count: usize) -> String {
    text.lines()
        .rev()
        .take(count)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n")
}

fn _parse_prepare_output(
    combined: &str,
) -> (
    Vec<String>,
    Vec<String>,
    bool,
    Option<serde_json::Value>,
    Vec<String>,
) {
    let mut warnings = Vec::new();
    let mut errors = Vec::new();
    let mut prep_done = false;
    let mut analysis = None;
    let mut analysis_errors = Vec::new();

    for line in combined.lines() {
        if let Some(msg) = line.strip_prefix("PREP_WARN:") {
            warnings.push(msg.trim().to_string());
            continue;
        }
        if let Some(msg) = line.strip_prefix("PREP_ERROR:") {
            errors.push(msg.trim().to_string());
            continue;
        }
        if line.split_whitespace().any(|token| token == "PREP_DONE") {
            prep_done = true;
            continue;
        }
        if let Some(idx) = line.find("PCR_ANALYSIS_JSON:") {
            let payload = &line[idx + "PCR_ANALYSIS_JSON:".len()..];
            match serde_json::from_str::<serde_json::Value>(payload.trim()) {
                Ok(parsed) => analysis = Some(parsed),
                Err(err) => analysis_errors.push(format!("Invalid analysis JSON returned by Blender: {err}")),
            }
        }
    }

    (warnings, errors, prep_done, analysis, analysis_errors)
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
        let ex = work_dir.join("extracted");
        fs::create_dir_all(&ex).map_err(|e| e.to_string())?;
        _extract_zip_to_dir(source, &ex)?;
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

    let (mut warnings, mut errors, prep_done_from_logs, _, analysis_parse_errors) = _parse_prepare_output(&combined);
    for err in analysis_parse_errors {
        warnings.push(format!("Analysis metadata warning: {err}"));
    }
    if !output.status.success() {
        errors.push(format!(
            "Blender prepare failed (exit code: {}). Last logs:\n{}",
            output.status.code().unwrap_or(-1),
            _tail_lines(&combined, 20)
        ));
    }
    let prep_done = output.status.success() && (prep_done_from_logs || errors.is_empty());

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

#[tauri::command]
pub async fn analyze_and_prepare_blend(
    file_path: String,
    blender_bin: String,
) -> Result<AnalyzeAndPrepareResult, String> {
    let source = Path::new(&file_path);
    if !source.is_file() {
        return Err("Selected path is not a file.".to_string());
    }
    if blender_bin.trim().is_empty() {
        return Err("Blender binary path is empty".to_string());
    }

    let filename = source
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or("Invalid file path")?
        .to_string();

    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let work_dir = std::env::temp_dir()
        .join("pcrent_analyze")
        .join(format!("job_{ts}"));
    fs::create_dir_all(&work_dir).map_err(|e| format!("Cannot create analyze work dir: {e}"))?;

    let prep_script = work_dir.join("prepare_blend.py");
    fs::write(&prep_script, PREPARE_BLEND_PY)
        .map_err(|e| format!("Cannot write analyze/prepare script: {e}"))?;

    let is_zip = filename.to_lowercase().ends_with(".zip");
    let blend_path: PathBuf;
    let extract_dir: Option<PathBuf>;

    if is_zip {
        let ex = work_dir.join("extracted");
        fs::create_dir_all(&ex).map_err(|e| e.to_string())?;
        _extract_zip_to_dir(source, &ex)?;

        let blends = _find_blend_files(&ex);
        if blends.is_empty() {
            let analysis_errors = vec!["No .blend file found inside zip".to_string()];
            let prepare_warnings = Vec::new();
            let prepare_errors = Vec::new();
            let warnings = prepare_warnings.clone();
            let mut errors = analysis_errors.clone();
            errors.extend(prepare_errors.clone());
            return Ok(AnalyzeAndPrepareResult {
                analysis: None,
                prepared_path: None,
                filename,
                analysis_warnings: Vec::new(),
                analysis_errors,
                prepare_warnings,
                prepare_errors,
                warnings,
                errors,
                prep_done: false,
            });
        }
        let source_stem = Path::new(&filename)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        blend_path = _choose_target_blend(source_stem, &ex, &blends);
        extract_dir = Some(ex);
    } else {
        let dest = work_dir.join(&filename);
        fs::copy(source, &dest).map_err(|e| format!("Cannot copy blend: {e}"))?;
        blend_path = dest;
        extract_dir = None;
    }

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

    let (
        prepare_warnings,
        mut prepare_errors,
        prep_done_from_logs,
        mut analysis,
        mut analysis_errors,
    ) = _parse_prepare_output(&combined);
    let mut analysis_warnings: Vec<String> = Vec::new();

    if !output.status.success() {
        prepare_errors.push(format!(
            "Blender analyze/prepare failed (exit code: {}). Last logs:\n{}",
            output.status.code().unwrap_or(-1),
            _tail_lines(&combined, 20)
        ));
    }
    if analysis.is_none() {
        let fallback_blend_path = blend_path.to_string_lossy().to_string();
        match analyze_blend_with_blender(fallback_blend_path, blender_bin.clone()).await {
            Ok(parsed) => {
                analysis = Some(parsed);
                if !analysis_errors.is_empty() {
                    analysis_warnings.extend(
                        analysis_errors
                            .drain(..)
                            .map(|msg| format!("Primary analysis payload issue (recovered by fallback): {msg}")),
                    );
                }
            }
            Err(err) => {
                analysis_errors.push(format!(
                    "Analysis metadata not returned by Blender. Fallback analysis failed: {err}"
                ));
            }
        }
    }

    let mut prep_done = output.status.success() && (prep_done_from_logs || prepare_errors.is_empty());
    let prepared_path = if prep_done {
        if is_zip {
            let new_zip = work_dir.join(&filename);
            if let Some(ex) = extract_dir.as_ref() {
                match _zip_dir(ex, &new_zip) {
                    Ok(()) => Some(new_zip.to_string_lossy().to_string()),
                    Err(err) => {
                        prepare_errors.push(format!("Prepared zip packaging failed: {err}"));
                        prep_done = false;
                        None
                    }
                }
            } else {
                prep_done = false;
                prepare_errors.push("Prepared zip packaging failed: extracted bundle missing".to_string());
                None
            }
        } else {
            Some(blend_path.to_string_lossy().to_string())
        }
    } else {
        None
    };

    let mut warnings = prepare_warnings.clone();
    warnings.extend(analysis_warnings.clone());
    let mut errors = prepare_errors.clone();
    errors.extend(analysis_errors.clone());

    Ok(AnalyzeAndPrepareResult {
        analysis,
        prepared_path,
        filename,
        analysis_warnings,
        analysis_errors,
        prepare_warnings,
        prepare_errors,
        warnings,
        errors,
        prep_done,
    })
}

#[tauri::command]
pub async fn analyze_blend_with_blender(
    file_path: String,
    blender_bin: String,
) -> Result<serde_json::Value, String> {
    let source = Path::new(&file_path);
    if !source.is_file() {
        return Err("Selected path is not a file.".to_string());
    }
    if blender_bin.trim().is_empty() {
        return Err("Blender binary path is empty".to_string());
    }

    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let work_dir = std::env::temp_dir()
        .join("pcrent_probe")
        .join(format!("job_{ts}"));
    fs::create_dir_all(&work_dir).map_err(|e| format!("Cannot create analyze work dir: {e}"))?;

    let script_path = work_dir.join("analyze_blend.py");
    fs::write(&script_path, ANALYZE_BLEND_PY)
        .map_err(|e| format!("Cannot write analyze script: {e}"))?;

    let output = std::process::Command::new(&blender_bin)
        .arg("-b")
        .arg(source)
        .arg("--python")
        .arg(&script_path)
        .output()
        .map_err(|e| format!("Failed to launch Blender for analysis: {e}"))?;

    let combined = format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let prefix = "PCR_ANALYSIS_JSON:";
    let maybe_line = combined.lines().find_map(|line| {
        line.find(prefix)
            .map(|idx| line[idx + prefix.len()..].trim().to_string())
    });

    let json_line = if let Some(line) = maybe_line {
        line
    } else {
        let tail = combined
            .lines()
            .rev()
            .take(20)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<Vec<_>>()
            .join("\n");
        if output.status.success() {
            return Err(format!(
                "Blender analysis did not return metadata. Last logs:\n{tail}"
            ));
        }
        return Err(format!(
            "Blender analysis failed (exit code: {}). Last logs:\n{}",
            output.status.code().unwrap_or(-1),
            tail
        ));
    };

    serde_json::from_str::<serde_json::Value>(&json_line)
        .map_err(|e| format!("Invalid Blender analysis JSON: {e}"))
}
