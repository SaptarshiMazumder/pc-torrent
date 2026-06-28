import { invoke } from "@tauri-apps/api/core";

export async function connectAgent(backendUrl, firebaseToken = "", commitmentSeconds = 0) {
  // commitmentSeconds is the user-chosen availability window from the
  // dashboard datetime picker.  Server's /machines/register rejects
  // anything <= 0 with 400; the agent sidecar also short-circuits with
  // a clear error if it's missing.  Float so sub-second precision flows
  // through cleanly to the server's commitment_end_at timestamp math.
  return invoke("connect_agent", {
    backendUrl,
    firebaseToken,
    commitmentSeconds,
  });
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
  const {
    jobFolder = null,
    typeSubfolder = null,
    preferredFilename = null,
    expectedSizeBytes = null,
    overwriteExisting = true,
  } = options;
  return invoke("download_job_output_to_downloads", {
    url,
    jobFolder,
    typeSubfolder,
    preferredFilename,
    expectedSizeBytes,
    overwriteExisting,
  });
}

/**
 * Cache a full-res frame to the app cache dir (frames-full/).
 * Returns the local file path. Skips download if already cached with matching size.
 */
export async function cacheViewerFrame(url, cacheKey, expectedSizeBytes = null) {
  return invoke("cache_viewer_frame", { url, cacheKey, expectedSizeBytes });
}
