use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

const MAX_LOGS: usize = 500;

#[derive(Debug, Clone, Serialize, Deserialize)]
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
pub struct JobInfo {
    pub job_id: String,
    pub filename: String,
    pub status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEntry {
    pub level: String,
    pub source: String,
    pub message: String,
}

pub struct AgentState {
    pub status: String,
    pub message: String,
    pub machine_id: String,
    pub system_info: SystemInfo,
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
