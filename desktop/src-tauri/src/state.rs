use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

const MAX_LOGS: usize = 500;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SystemInfo {
    pub gpu_name: String,
    pub gpu_vram_gb: f64,
    pub cpu_cores: u32,
    pub ram_gb: f64,
    pub os_version: String,
    pub nvidia_driver: String,
    pub ready: bool,
    pub issues: Vec<String>,
}

impl Default for SystemInfo {
    fn default() -> Self {
        Self {
            gpu_name: String::new(),
            gpu_vram_gb: 0.0,
            cpu_cores: 0,
            ram_gb: 0.0,
            os_version: String::new(),
            nvidia_driver: String::new(),
            ready: false,
            issues: vec![],
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct JobInfo {
    pub job_id: String,
    pub filename: String,
    pub status: String,
    pub current_frame: Option<u32>,
    pub rendered_frames: Option<u32>,
    pub total_frames: Option<u32>,
    pub progress_pct: Option<f64>,
}

impl Default for JobInfo {
    fn default() -> Self {
        Self {
            job_id: String::new(),
            filename: String::new(),
            status: "rendering".to_string(),
            current_frame: None,
            rendered_frames: None,
            total_frames: None,
            progress_pct: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEntry {
    pub level: String,
    pub source: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RuntimeInfo {
    pub preflight_complete: bool,
    pub preflight_passed: Option<bool>,
    pub preflight_message: String,
    pub requirements_checked: bool,
    pub requirements_ready: Option<bool>,
    pub requirement_issues: Vec<String>,
    pub wsl_ready: Option<bool>,
    pub docker_installed: Option<bool>,
    pub docker_running: Option<bool>,
    pub gpu_verified: Option<bool>,
    pub image_present: Option<bool>,
    pub image_stage: String,
    pub image_downloaded_bytes: Option<u64>,
    pub image_total_bytes: Option<u64>,
    pub image_progress_pct: Option<f64>,
    pub image_status: String,
}

impl Default for RuntimeInfo {
    fn default() -> Self {
        Self {
            preflight_complete: false,
            preflight_passed: None,
            preflight_message: "Preflight has not run yet.".to_string(),
            requirements_checked: false,
            requirements_ready: None,
            requirement_issues: vec![],
            wsl_ready: None,
            docker_installed: None,
            docker_running: None,
            gpu_verified: None,
            image_present: None,
            image_stage: "idle".to_string(),
            image_downloaded_bytes: None,
            image_total_bytes: None,
            image_progress_pct: None,
            image_status: "Not checked yet.".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AgentState {
    pub status: String,
    pub message: String,
    pub machine_id: String,
    pub system_info: SystemInfo,
    pub runtime_info: RuntimeInfo,
    pub current_job: Option<JobInfo>,
    pub logs: VecDeque<LogEntry>,
}

impl Default for AgentState {
    fn default() -> Self {
        Self {
            status: "disconnected".to_string(),
            message: "Ready".to_string(),
            machine_id: String::new(),
            system_info: SystemInfo::default(),
            runtime_info: RuntimeInfo::default(),
            current_job: None,
            logs: VecDeque::with_capacity(MAX_LOGS),
        }
    }
}

impl AgentState {
    pub fn push_log(&mut self, entry: LogEntry) {
        if self.logs.len() >= MAX_LOGS {
            self.logs.pop_front();
        }
        self.logs.push_back(entry);
    }
}
