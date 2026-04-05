import { auth } from "./firebase/config";

const BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function authHeaders() {
  const token = await auth.currentUser?.getIdToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiFetch(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
    ...(await authHeaders()),
  };
  const r = await fetch(`${BASE}${path}`, { ...options, headers });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed: ${r.status}`);
  }
  return r.json();
}

async function uploadFileToPresignedUrl(uploadUrl, file, onProgress) {
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`Upload failed: ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(file);
  });
}

export async function getMe() {
  return apiFetch("/me");
}

export async function updateMe(fields) {
  return apiFetch("/me", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
}

export async function getMachines() {
  return apiFetch("/machines");
}

export async function submitJob(machineId, file, onProgress) {
  const { job_id, upload_url } = await apiFetch("/jobs/request-upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_id: machineId, filename: file.name }),
  });

  await uploadFileToPresignedUrl(upload_url, file, onProgress);

  await apiFetch(`/jobs/${job_id}/confirm-upload`, { method: "POST" });

  return { job_id, status: "pending" };
}

export async function listJobs() {
  return apiFetch("/jobs");
}

export async function getJob(jobId) {
  return apiFetch(`/jobs/${jobId}`);
}

export async function listRenderGroups() {
  return apiFetch("/render-groups");
}

export function downloadUrl(jobId) {
  return `${BASE}/jobs/${jobId}/download`;
}

// ---- Distributed Rendering (Render Groups) ----

export async function createDistributedRenderGroup(machineIds, filename) {
  return apiFetch("/render-groups/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_ids: machineIds, filename }),
  });
}

export async function uploadDistributedRenderInput(uploadUrl, file, onProgress) {
  await uploadFileToPresignedUrl(uploadUrl, file, onProgress);
  if (onProgress) onProgress(100);
}

export async function confirmDistributedJob(groupId, machineIds, frameRange = null) {
  const body = { machine_ids: machineIds };
  if (frameRange) {
    body.frame_start = frameRange.frame_start;
    body.frame_end = frameRange.frame_end;
    body.frame_step = frameRange.frame_step || 1;
  }
  return apiFetch(`/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getRenderGroup(groupId) {
  return apiFetch(`/render-groups/${groupId}`);
}

export function renderGroupDownloadUrl(groupId) {
  return `${BASE}/render-groups/${groupId}/download`;
}

export async function logsStreamUrl() {
  const token = await auth.currentUser?.getIdToken();
  const url = new URL(`${BASE}/logs/stream`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}
