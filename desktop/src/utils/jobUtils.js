export const STATUS_LABELS = {
  pending: "Pending",
  uploading: "Uploading",
  running: "Rendering",
  done: "Done",
  cancelled: "Cancelled",
  failed: "Failed",
};

export const TERMINAL_STATUSES = new Set(["done", "cancelled", "failed"]);

export function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(status);
}

export function terminalFallbackPct(status) {
  if (status === "done") return 100;
  if (status === "cancelled" || status === "failed") return 0;
  return null;
}

export function resolveJobFilename(job) {
  if (typeof job?.filename === "string" && job.filename.trim()) return job.filename.trim();
  if (typeof job?.input_filename === "string" && job.input_filename.trim()) return job.input_filename.trim();
  return "Untitled";
}

export function buildDownloadFolderName(jobFilename, id) {
  const rawName = typeof jobFilename === "string" && jobFilename.trim() ? jobFilename.trim() : "render";
  const baseName = rawName.replace(/\.[^/.]+$/, "");
  const suffix = typeof id === "string" && id ? id.slice(0, 8) : "job";
  return `render__${baseName}__${suffix}`;
}

export function frameIndexFromFilename(filename) {
  if (typeof filename !== "string") return -1;
  const match = filename.match(/(\d+)(?=\.[^.]+$)/);
  if (!match) return -1;
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : -1;
}

export function outputSort(a, b) {
  const frameA = frameIndexFromFilename(a?.filename || "");
  const frameB = frameIndexFromFilename(b?.filename || "");
  if (frameA !== frameB) return frameA - frameB;
  return String(a?.filename || "").localeCompare(String(b?.filename || ""));
}

export function jobKey(job) {
  return job?.group_id || job?.job_id || "";
}

export function buildAuthenticatedUrl(baseUrl, path, token, cacheBuster = null) {
  if (!baseUrl || !path) return "";
  const normalizedBase = String(baseUrl).trim().replace(/\/+$/, "");
  const url = new URL(path, `${normalizedBase}/`);
  if (token) url.searchParams.set("token", token);
  if (cacheBuster !== null && cacheBuster !== undefined) {
    url.searchParams.set("v", String(cacheBuster));
  }
  return url.toString();
}

export function summarizeDownloadActions(actions) {
  const parts = [];
  if (actions.downloaded) parts.push(`${actions.downloaded} downloaded`);
  if (actions.skipped) parts.push(`${actions.skipped} already had`);
  if (actions.failed) parts.push(`${actions.failed} failed`);
  return parts.join(", ") || "done";
}

// Finds the task in a render group with the highest-index output frame
export function getLatestTaskWithOutput(tasks) {
  if (!Array.isArray(tasks)) return null;
  return tasks.reduce((best, task) => {
    if (!task?.latest_output_file) return best;
    if (!best?.latest_output_file) return task;
    const bestFrame = frameIndexFromFilename(best.latest_output_file);
    const taskFrame = frameIndexFromFilename(task.latest_output_file);
    if (taskFrame > bestFrame) return task;
    if (taskFrame === bestFrame && task.latest_output_file > best.latest_output_file) return task;
    return best;
  }, null);
}

// Constructs a preview URL for a render group's latest frame
export function getGroupPreviewUrl(job, backendUrl, authToken) {
  const latestTask = getLatestTaskWithOutput(job?.tasks);
  if (!latestTask?.latest_output_file) return "";
  const availableCount =
    typeof job?.available_output_files_count === "number"
      ? job.available_output_files_count
      : (job?.tasks || []).reduce((sum, t) => sum + (t.output_files_count || 0), 0);
  return buildAuthenticatedUrl(
    backendUrl,
    `/jobs/${latestTask.job_id}/output/${encodeURIComponent(latestTask.latest_output_file)}/preview`,
    authToken,
    availableCount
  );
}

// Constructs a preview URL for a single job's latest frame
export function getSingleJobPreviewUrl(job, backendUrl, authToken) {
  const outputFiles = Array.isArray(job?.output_files) ? job.output_files : [];
  const latestFile =
    job?.latest_output_file ||
    outputFiles.slice().sort((a, b) => frameIndexFromFilename(a) - frameIndexFromFilename(b)).pop() ||
    "";
  if (!latestFile || !backendUrl) return "";
  const availableCount =
    typeof job?.output_files_count === "number" ? job.output_files_count : outputFiles.length;
  return buildAuthenticatedUrl(
    backendUrl,
    `/jobs/${job.job_id}/output/${encodeURIComponent(latestFile)}/preview`,
    authToken,
    availableCount
  );
}
