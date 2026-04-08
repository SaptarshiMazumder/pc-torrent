import { auth } from "./firebase/config";

const BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");

async function authHeaders() {
  try {
    const token = await auth.currentUser?.getIdToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch (error) {
    const detail = error?.message || String(error);
    throw new Error(`Auth token refresh failed: ${detail}`);
  }
}

async function readErrorDetail(response, fallbackMessage) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      const payload = await response.json();
      if (payload?.detail) return payload.detail;
      if (payload?.error) return payload.error;
    } catch {
      // ignore parse failures
    }
  }

  try {
    const text = await response.text();
    if (text && text.trim()) return text.trim();
  } catch {
    // ignore text read failures
  }

  return fallbackMessage;
}

async function apiFetch(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
    ...(await authHeaders()),
  };

  let response;
  try {
    response = await fetch(`${BASE}${path}`, { ...options, headers });
  } catch (error) {
    const detail = error?.message || String(error);
    throw new Error(`Network error reaching ${BASE}${path}: ${detail}`);
  }
  if (!response.ok) {
    const detail = await readErrorDetail(response, `Request failed (${response.status})`);
    throw new Error(detail);
  }

  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

function isMultipartEndpointMissing(error) {
  const message = String(error?.message || "").toLowerCase();
  return message.includes("404") && message.includes("multipart-upload");
}

async function uploadFileToPresignedUrl(uploadUrl, file, onProgress, signal = null) {
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

    const cleanup = () => {
      if (signal && abortListener) signal.removeEventListener("abort", abortListener);
    };

    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }

    xhr.onload = () => {
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`Upload failed with status ${xhr.status}`));
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Upload aborted", "AbortError"));
    };
    xhr.onerror = () => {
      cleanup();
      reject(new Error(aborted ? "Upload aborted" : "Upload failed"));
    };
    xhr.send(file);
  });
}

async function uploadBlobPart(partUrl, blob, signal = null, onBytes = null) {
  return await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let aborted = false;
    let abortListener = null;
    let uploaded = 0;

    xhr.open("PUT", partUrl);
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

    const cleanup = () => {
      if (signal && abortListener) signal.removeEventListener("abort", abortListener);
    };

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const delta = Math.max(0, e.loaded - uploaded);
      uploaded = e.loaded;
      if (delta > 0 && onBytes) onBytes(delta);
    };

    xhr.onload = () => {
      cleanup();
      if (uploaded < blob.size && onBytes) onBytes(blob.size - uploaded);
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader("etag") || xhr.getResponseHeader("ETag");
        if (!etag) {
          reject(new Error("Missing ETag in multipart part response"));
          return;
        }
        resolve(etag);
        return;
      }
      reject(new Error(`Part upload failed with status ${xhr.status}`));
    };

    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Upload aborted", "AbortError"));
    };
    xhr.onerror = () => {
      cleanup();
      reject(new Error(aborted ? "Upload aborted" : "Part upload failed"));
    };

    xhr.send(blob);
  });
}

async function uploadMultipartInput(kind, id, file, onProgress, signal = null) {
  const init = await apiFetch(`/${kind}/${id}/multipart-upload/init`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file_size_bytes: file.size,
      content_type: "application/octet-stream",
    }),
    signal,
  });

  const partSize = Math.max(5 * 1024 * 1024, Number(init.part_size_bytes || 0));
  const totalParts = Math.max(1, Number(init.total_parts || 0));

  let uploadedBytes = 0;
  const reportProgress = () => {
    if (!onProgress) return;
    const pct = Math.min(100, Math.max(0, Math.round((uploadedBytes / file.size) * 100)));
    onProgress(pct);
  };
  reportProgress();

  const completedParts = [];
  const partUrlCache = new Map();
  const partUrlBatchSize = 16;

  try {
    for (let partNumber = 1; partNumber <= totalParts; partNumber += 1) {
      if (!partUrlCache.has(partNumber)) {
        const batch = [];
        for (let n = partNumber; n <= totalParts && n < partNumber + partUrlBatchSize; n += 1) {
          batch.push(n);
        }

        const partUrlResp = await apiFetch(`/${kind}/${id}/multipart-upload/part-urls`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            upload_id: init.upload_id,
            part_numbers: batch,
          }),
          signal,
        });
        const entries = Object.entries(partUrlResp.urls || {});
        for (const [k, v] of entries) {
          const numeric = Number.parseInt(k, 10);
          if (Number.isInteger(numeric) && typeof v === "string") {
            partUrlCache.set(numeric, v);
          }
        }
      }

      const partUrl = partUrlCache.get(partNumber);
      if (!partUrl) throw new Error(`Missing upload URL for part ${partNumber}`);
      partUrlCache.delete(partNumber);

      const start = (partNumber - 1) * partSize;
      const end = Math.min(file.size, start + partSize);
      const blob = file.slice(start, end);
      const etag = await uploadBlobPart(partUrl, blob, signal, (delta) => {
        uploadedBytes += delta;
        reportProgress();
      });
      completedParts.push({ part_number: partNumber, etag });
    }

    await apiFetch(`/${kind}/${id}/multipart-upload/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: init.upload_id,
        parts: completedParts,
      }),
      signal,
    });
    if (onProgress) onProgress(100);
  } catch (error) {
    await apiFetch(`/${kind}/${id}/multipart-upload/abort`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_id: init.upload_id }),
    }).catch(() => {});
    throw error;
  }
}

export async function getMe() {
  return apiFetch("/me");
}

export async function getFirebaseToken() {
  return auth.currentUser?.getIdToken() ?? null;
}

export function buildAuthenticatedApiUrl(path, token = "", cacheBuster = null) {
  const url = new URL(path, `${BASE}/`);
  if (token) {
    url.searchParams.set("token", token);
  }
  if (cacheBuster !== null && cacheBuster !== undefined) {
    url.searchParams.set("v", String(cacheBuster));
  }
  return url.toString();
}

export async function listInputFiles() {
  return apiFetch("/me/input-files");
}

export async function renameInputFile(assetId, displayName) {
  return apiFetch(`/me/input-files/${assetId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function deleteInputFile(assetId) {
  return apiFetch(`/me/input-files/${assetId}`, {
    method: "DELETE",
  });
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

export async function submitJob(machineId, file, onProgress, signal = null) {
  const requestInfo = await apiFetch("/jobs/request-upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      machine_id: machineId,
      filename: file.name,
      file_size_bytes: file.size,
    }),
    signal,
  });
  const jobId = requestInfo.job_id;

  try {
    await uploadMultipartInput("jobs", jobId, file, onProgress, signal);
  } catch (error) {
    const canFallback =
      typeof requestInfo.upload_url === "string" &&
      file.size <= Number(requestInfo.single_put_max_bytes || 5 * 1024 * 1024 * 1024) &&
      isMultipartEndpointMissing(error);
    if (!canFallback) throw error;
    await uploadFileToPresignedUrl(requestInfo.upload_url, file, onProgress, signal);
    if (onProgress) onProgress(100);
  }

  await apiFetch(`/jobs/${jobId}/confirm-upload`, { method: "POST", signal });
  return { job_id: jobId, status: "pending" };
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

export async function createDistributedRenderGroup(
  machineIds,
  filename = null,
  fileSizeBytes = null,
  sourceAssetId = null
) {
  const body = {
    machine_ids: machineIds,
  };
  if (sourceAssetId) {
    body.source_asset_id = sourceAssetId;
  } else {
    body.filename = filename;
    body.file_size_bytes = fileSizeBytes;
  }
  return apiFetch("/render-groups/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function uploadDistributedRenderInput(groupId, file, onProgress, signal = null) {
  await uploadMultipartInput("render-groups", groupId, file, onProgress, signal);
}

export async function confirmDistributedJob(
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
  if (renderOverrides) body.render_overrides = renderOverrides;
  if (scheduling) body.scheduling = scheduling;
  if (analysisSnapshot) body.analysis_snapshot = analysisSnapshot;

  return apiFetch(`/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function getRenderGroup(groupId) {
  return apiFetch(`/render-groups/${groupId}`);
}

export function renderGroupDownloadUrl(groupId) {
  return `${BASE}/render-groups/${groupId}/download`;
}

export function renderGroupOutputsUrl(groupId) {
  return `${BASE}/render-groups/${groupId}/outputs`;
}

export async function getRenderGroupOutputs(groupId) {
  return apiFetch(`/render-groups/${groupId}/outputs`);
}

export async function getJobOutputs(jobId) {
  return apiFetch(`/jobs/${jobId}/outputs`);
}

export async function cancelRenderGroup(groupId) {
  return apiFetch(`/render-groups/${groupId}/cancel`, { method: "POST" });
}

export async function logsStreamUrl() {
  const token = await auth.currentUser?.getIdToken();
  const url = new URL(`${BASE}/logs/stream`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}
