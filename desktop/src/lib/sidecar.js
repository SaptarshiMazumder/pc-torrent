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

export async function getSystemInfo() {
  return invoke("get_system_info");
}

export async function getRuntimeStatus() {
  return invoke("get_runtime_status");
}

export async function runPreflight() {
  return invoke("run_preflight");
}

export async function runWslSetup() {
  return invoke("run_wsl_setup");
}

export async function runDockerSetup() {
  return invoke("run_docker_setup");
}

export async function removeImage() {
  return invoke("remove_image");
}

export async function pickProjectFile() {
  return invoke("pick_project_file");
}

export async function analyzeProjectFile(filePath) {
  return invoke("analyze_project_file", { filePath });
}

export async function uploadProjectFile(filePath, uploadUrl) {
  return invoke("upload_project_file", { filePath, uploadUrl });
}

export async function downloadJobOutputToDownloads(url) {
  return invoke("download_job_output_to_downloads", { url });
}
