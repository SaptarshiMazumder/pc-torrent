import { invoke } from "@tauri-apps/api/core";

export async function connectAgent(backendUrl) {
  return invoke("connect_agent", { backendUrl });
}

export async function disconnectAgent() {
  return invoke("disconnect_agent");
}

export async function pauseAgent() {
  return invoke("pause_agent");
}

export async function resumeAgent() {
  return invoke("resume_agent");
}

export async function stopJob() {
  return invoke("stop_job");
}

export async function getAgentState() {
  return invoke("get_agent_state");
}

export async function clearLogs() {
  return invoke("clear_logs");
}

export async function getSystemInfo() {
  return invoke("get_system_info");
}

export async function getRuntimeStatus() {
  return invoke("get_runtime_status");
}

export async function runPreflight(force = false) {
  return invoke("run_preflight", { force });
}

export async function removeImage() {
  return invoke("remove_image");
}

export async function downloadJobOutputToDownloads(url, options = {}) {
  const { jobFolder = null, preferredFilename = null } = options;
  return invoke("download_job_output_to_downloads", {
    url,
    jobFolder,
    preferredFilename,
  });
}
