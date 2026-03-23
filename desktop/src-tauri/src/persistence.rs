use std::fs;
use std::io::ErrorKind;
use std::path::PathBuf;

use tauri::{AppHandle, Manager, Runtime};

use crate::state::AgentState;

const STATE_FILE_NAME: &str = "agent-state.json";

fn state_file_path<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_local_data_dir()
        .map_err(|e| format!("Failed to resolve app data directory: {e}"))?;
    Ok(dir.join(STATE_FILE_NAME))
}

pub fn load_agent_state<R: Runtime>(app: &AppHandle<R>) -> Result<AgentState, String> {
    let path = state_file_path(app)?;
    let raw = match fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(AgentState::default()),
        Err(err) => return Err(format!("Failed to read persisted state from {}: {err}", path.display())),
    };

    serde_json::from_str(&raw)
        .map_err(|err| format!("Failed to parse persisted state from {}: {err}", path.display()))
}

pub fn save_agent_state<R: Runtime>(app: &AppHandle<R>, state: &AgentState) -> Result<(), String> {
    let path = state_file_path(app)?;
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)
            .map_err(|err| format!("Failed to create app data directory {}: {err}", dir.display()))?;
    }

    let temp_path = path.with_extension("json.tmp");
    let payload = serde_json::to_vec_pretty(state)
        .map_err(|err| format!("Failed to serialize persisted state: {err}"))?;

    fs::write(&temp_path, payload)
        .map_err(|err| format!("Failed to write temporary state file {}: {err}", temp_path.display()))?;

    if path.exists() {
        fs::remove_file(&path)
            .map_err(|err| format!("Failed to replace state file {}: {err}", path.display()))?;
    }

    fs::rename(&temp_path, &path)
        .map_err(|err| format!("Failed to finalize state file {}: {err}", path.display()))?;

    Ok(())
}
