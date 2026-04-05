import { auth } from "../firebase/config";

async function authHeaders() {
  const token = await auth.currentUser?.getIdToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiFetch(baseUrl, path, options = {}) {
  const headers = {
    ...(options.headers || {}),
    ...(await authHeaders()),
  };
  const r = await fetch(`${baseUrl}${path}`, { ...options, headers });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed: ${r.status}`);
  }
  return r.json();
}

export async function getFirebaseToken() {
  return auth.currentUser?.getIdToken() ?? null;
}

export async function getMachines(baseUrl) {
  return apiFetch(baseUrl, "/machines");
}

export async function listJobs(baseUrl) {
  return apiFetch(baseUrl, "/jobs");
}

export async function listRenderGroups(baseUrl) {
  return apiFetch(baseUrl, "/render-groups");
}

async function uploadFileToPresignedUrl(uploadUrl, file, onProgress, signal = null) {
  let lastProgress = 0;
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let aborted = false;
    let abortListener = null;

    xhr.open("PUT", uploadUrl);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");

    if (signal?.aborted) {
      reject(new DOMException("Upload aborted", "AbortError"));
      return;
    }

    if (signal) {
      abortListener = () => {
        aborted = true;
        xhr.abort();
      };
      signal.addEventListener("abort", abortListener, { once: true });
    }

    const cleanupAbortListener = () => {
      if (signal && abortListener) {
        signal.removeEventListener("abort", abortListener);
      }
    };

    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const nextProgress = Math.round((e.loaded / e.total) * 100);
          lastProgress = Math.max(lastProgress, nextProgress);
          onProgress(lastProgress);
        }
      };
    }
    xhr.onload = () => {
      cleanupAbortListener();
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`Upload failed with status ${xhr.status}`));
    };
    xhr.onabort = () => {
      cleanupAbortListener();
      reject(new DOMException("Upload aborted", "AbortError"));
    };
    xhr.onerror = () => {
      cleanupAbortListener();
      reject(new Error(aborted ? "Upload aborted" : "Upload failed"));
    };
    xhr.send(file);
  });
}

export async function submitJob(baseUrl, machineId, file, onProgress) {
  const { job_id, upload_url } = await apiFetch(baseUrl, "/jobs/request-upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_id: machineId, filename: file.name }),
  });

  await uploadFileToPresignedUrl(upload_url, file, onProgress);

  await apiFetch(baseUrl, `/jobs/${job_id}/confirm-upload`, { method: "POST" });

  return { job_id, status: "pending" };
}

export async function getJob(baseUrl, jobId) {
  return apiFetch(baseUrl, `/jobs/${jobId}`);
}

export function downloadUrl(baseUrl, jobId) {
  return `${baseUrl}/jobs/${jobId}/download`;
}

export function jobOutputsUrl(baseUrl, jobId) {
  return `${baseUrl}/jobs/${jobId}/outputs`;
}

// ---- Distributed Rendering (Render Groups) ----

export async function createDistributedRenderGroup(baseUrl, machineIds, filename) {
  return apiFetch(baseUrl, "/render-groups/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_ids: machineIds, filename }),
  });
}

export async function uploadDistributedRenderInput(uploadUrl, file, onProgress, signal = null) {
  await uploadFileToPresignedUrl(uploadUrl, file, onProgress, signal);
  if (onProgress) onProgress(100);
}

export async function confirmDistributedJob(
  baseUrl,
  groupId,
  machineIds,
  frameRange = null,
  renderOverrides = null,
  scheduling = null,
  analysisSnapshot = null,
  signal = null
) {
  const body = { machine_ids: machineIds };
  if (frameRange) {
    body.frame_start = frameRange.frame_start;
    body.frame_end = frameRange.frame_end;
    body.frame_step = frameRange.frame_step || 1;
  }
  if (renderOverrides) {
    body.render_overrides = renderOverrides;
  }
  if (scheduling) {
    body.scheduling = scheduling;
  }
  if (analysisSnapshot) {
    body.analysis_snapshot = analysisSnapshot;
  }
  return apiFetch(baseUrl, `/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function getRenderGroup(baseUrl, groupId) {
  return apiFetch(baseUrl, `/render-groups/${groupId}`);
}

export function renderGroupDownloadUrl(baseUrl, groupId) {
  return `${baseUrl}/render-groups/${groupId}/download`;
}

export function renderGroupOutputsUrl(baseUrl, groupId) {
  return `${baseUrl}/render-groups/${groupId}/outputs`;
}

export async function getRenderGroupOutputs(baseUrl, groupId) {
  return apiFetch(baseUrl, `/render-groups/${groupId}/outputs`);
}

export async function getJobOutputs(baseUrl, jobId) {
  return apiFetch(baseUrl, `/jobs/${jobId}/outputs`);
}

export async function cancelRenderGroup(baseUrl, groupId) {
  return apiFetch(baseUrl, `/render-groups/${groupId}/cancel`, { method: "POST" });
}
