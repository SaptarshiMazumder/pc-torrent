use serde_json::Value;
use std::sync::Arc;
use tauri::AppHandle;
use tauri::Emitter;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tokio::sync::Mutex;

use crate::persistence::save_agent_state;
use crate::state::{AgentState, JobInfo, LogEntry, RuntimeInfo, SystemInfo};

pub struct SidecarHandle {
    child: Option<CommandChild>,
}

impl SidecarHandle {
    pub fn new() -> Self {
        Self { child: None }
    }

    pub fn is_running(&self) -> bool {
        self.child.is_some()
    }

    pub fn send_command(&mut self, cmd: &Value) -> Result<(), String> {
        if let Some(ref mut child) = self.child {
            let line = serde_json::to_string(cmd).map_err(|e| e.to_string())?;
            child
                .write((line + "\n").as_bytes())
                .map_err(|e| e.to_string())?;
            Ok(())
        } else {
            Err("Sidecar not running".to_string())
        }
    }

    pub fn kill(&mut self) {
        if let Some(child) = self.child.take() {
            let _ = child.kill();
        }
    }
}

pub fn spawn_sidecar(
    app: &AppHandle,
    state: Arc<Mutex<AgentState>>,
    sidecar_handle: Arc<Mutex<SidecarHandle>>,
) -> Result<(), String> {
    let shell = app.shell();
    let cmd = shell
        .sidecar("pcrent-agent")
        .map_err(|e| format!("Failed to create sidecar command: {}", e))?;

    let app_handle = app.clone();

    let (mut rx, child) = cmd.spawn().map_err(|e| format!("Failed to spawn sidecar: {}", e))?;

    // Store the child handle
    {
        let handle = sidecar_handle.clone();
        tauri::async_runtime::spawn(async move {
            handle.lock().await.child = Some(child);
        });
    }

    // Process sidecar output in background
    let state_clone = state.clone();
    let sidecar_handle_clone = sidecar_handle.clone();
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_shell::process::CommandEvent;

        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let line = String::from_utf8_lossy(&line);
                    let line = line.trim();
                    if line.is_empty() {
                        continue;
                    }
                    if let Ok(parsed) = serde_json::from_str::<Value>(line) {
                        handle_sidecar_event(&app_handle, &state_clone, &parsed).await;
                    }
                }
                CommandEvent::Stderr(line) => {
                    let line = String::from_utf8_lossy(&line);
                    eprintln!("[sidecar stderr] {}", line.trim());
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!("[sidecar] terminated: {:?}", payload);
                    sidecar_handle_clone.lock().await.child = None;
                    let snapshot = {
                        let mut s = state_clone.lock().await;
                        s.status = "disconnected".to_string();
                        s.message = "Agent process exited".to_string();
                        s.clone()
                    };
                    if let Err(err) = save_agent_state(&app_handle, &snapshot) {
                        eprintln!("[state] {err}");
                    }
                    let _ = app_handle.emit("agent-event", serde_json::json!({
                        "event": "status",
                        "state": "disconnected",
                        "message": "Agent process exited"
                    }));
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(())
}

async fn handle_sidecar_event(
    app: &AppHandle,
    state: &Arc<Mutex<AgentState>>,
    event: &Value,
) {
    let event_type = event.get("event").and_then(|v| v.as_str()).unwrap_or("");
    let snapshot = {
        let mut s = state.lock().await;

        match event_type {
            "status" => {
                let new_state = event.get("state").and_then(|v| v.as_str()).unwrap_or("");
                let message = event.get("message").and_then(|v| v.as_str()).unwrap_or("");
                let machine_id = event.get("machine_id").and_then(|v| v.as_str()).unwrap_or("");
                let job_id = event.get("job_id").and_then(|v| v.as_str()).unwrap_or("");
                let filename = event.get("filename").and_then(|v| v.as_str()).unwrap_or("");

                s.status = new_state.to_string();
                s.message = message.to_string();
                if !machine_id.is_empty() {
                    s.machine_id = machine_id.to_string();
                }
                if new_state == "rendering" && !job_id.is_empty() {
                    s.current_job = Some(JobInfo {
                        job_id: job_id.to_string(),
                        filename: filename.to_string(),
                        status: "rendering".to_string(),
                    });
                } else if new_state == "connected" || new_state == "disconnected" {
                    s.current_job = None;
                }
            }
            "system_info" => {
                s.system_info = SystemInfo {
                    gpu_name: event.get("gpu_name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    gpu_vram_gb: event.get("gpu_vram_gb").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    cpu_cores: event.get("cpu_cores").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
                    ram_gb: event.get("ram_gb").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    os_version: event.get("os_version").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    nvidia_driver: event.get("nvidia_driver").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    ready: event.get("ready").and_then(|v| v.as_bool()).unwrap_or(false),
                    issues: event.get("issues")
                        .and_then(|v| v.as_array())
                        .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                        .unwrap_or_default(),
                };
            }
            "runtime_info" => {
                s.runtime_info = RuntimeInfo {
                    preflight_complete: event.get("preflight_complete").and_then(|v| v.as_bool()).unwrap_or(false),
                    preflight_passed: event.get("preflight_passed").and_then(|v| v.as_bool()),
                    preflight_message: event.get("preflight_message").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    requirements_checked: event.get("requirements_checked").and_then(|v| v.as_bool()).unwrap_or(false),
                    requirements_ready: event.get("requirements_ready").and_then(|v| v.as_bool()),
                    requirement_issues: event
                        .get("requirement_issues")
                        .and_then(|v| v.as_array())
                        .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                        .unwrap_or_default(),
                    docker_installed: event.get("docker_installed").and_then(|v| v.as_bool()),
                    docker_running: event.get("docker_running").and_then(|v| v.as_bool()),
                    gpu_verified: event.get("gpu_verified").and_then(|v| v.as_bool()),
                    image_present: event.get("image_present").and_then(|v| v.as_bool()),
                    image_stage: event.get("image_stage").and_then(|v| v.as_str()).unwrap_or("idle").to_string(),
                    image_downloaded_bytes: event.get("image_downloaded_bytes").and_then(|v| v.as_u64()),
                    image_total_bytes: event.get("image_total_bytes").and_then(|v| v.as_u64()),
                    image_progress_pct: event.get("image_progress_pct").and_then(|v| v.as_f64()),
                    image_status: event.get("image_status").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                };
            }
            "log" => {
                s.push_log(LogEntry {
                    level: event.get("level").and_then(|v| v.as_str()).unwrap_or("info").to_string(),
                    source: event.get("source").and_then(|v| v.as_str()).unwrap_or("agent").to_string(),
                    message: event.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                });
            }
            "job_complete" => {
                s.current_job = None;
            }
            "error" => {
                s.push_log(LogEntry {
                    level: "error".to_string(),
                    source: "agent".to_string(),
                    message: event.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                });
            }
            _ => {}
        }

        s.clone()
    };

    if let Err(err) = save_agent_state(app, &snapshot) {
        eprintln!("[state] {err}");
    }

    // Forward every event to the React frontend
    let _ = app.emit("agent-event", event);
}
