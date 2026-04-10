import { auth } from "../firebase/config";

function normalizeBaseUrl(baseUrl) {
  return String(baseUrl || "").trim().replace(/\/+$/, "");
}

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

async function apiFetch(baseUrl, path, options = {}) {
  const normalizedBase = normalizeBaseUrl(baseUrl);
  if (!normalizedBase) {
    throw new Error("Backend URL is not configured");
  }

  const headers = {
    ...(options.headers || {}),
    ...(await authHeaders()),
  };

  let response;
  try {
    response = await fetch(`${normalizedBase}${path}`, { ...options, headers });
  } catch (error) {
    const detail = error?.message || String(error);
    throw new Error(`Network error reaching ${normalizedBase}${path}: ${detail}`);
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
      if (signal && abortListener) {
        signal.removeEventListener("abort", abortListener);
      }
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

async function uploadMultipartInput(baseUrl, kind, id, file, onProgress, signal = null) {
  const init = await apiFetch(baseUrl, `/${kind}/${id}/multipart-upload/init`, {
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

        const partUrlResp = await apiFetch(baseUrl, `/${kind}/${id}/multipart-upload/part-urls`, {
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

    await apiFetch(baseUrl, `/${kind}/${id}/multipart-upload/complete`, {
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
    await apiFetch(baseUrl, `/${kind}/${id}/multipart-upload/abort`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_id: init.upload_id }),
    }).catch(() => {});
    throw error;
  }
}

export async function getFirebaseToken() {
  return auth.currentUser?.getIdToken() ?? null;
}

export async function getMachines(baseUrl) {
  return apiFetch(baseUrl, "/machines");
}

export async function listInputFiles(baseUrl) {
  return apiFetch(baseUrl, "/me/input-files");
}

export async function renameInputFile(baseUrl, assetId, displayName) {
  return apiFetch(baseUrl, `/me/input-files/${assetId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function deleteInputFile(baseUrl, assetId) {
  return apiFetch(baseUrl, `/me/input-files/${assetId}`, {
    method: "DELETE",
  });
}

export async function listJobs(baseUrl) {
  return apiFetch(baseUrl, "/jobs");
}

export async function listRenderGroups(baseUrl) {
  return apiFetch(baseUrl, "/render-groups");
}

export async function submitJob(baseUrl, machineId, file, onProgress, signal = null) {
  const requestInfo = await apiFetch(baseUrl, "/jobs/request-upload", {
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
    await uploadMultipartInput(baseUrl, "jobs", jobId, file, onProgress, signal);
  } catch (error) {
    const canFallback =
      typeof requestInfo.upload_url === "string" &&
      file.size <= Number(requestInfo.single_put_max_bytes || 5 * 1024 * 1024 * 1024) &&
      isMultipartEndpointMissing(error);
    if (!canFallback) throw error;
    await uploadFileToPresignedUrl(requestInfo.upload_url, file, onProgress, signal);
    if (onProgress) onProgress(100);
  }

  await apiFetch(baseUrl, `/jobs/${jobId}/confirm-upload`, { method: "POST", signal });
  return { job_id: jobId, status: "pending" };
}

export async function getJob(baseUrl, jobId) {
  return apiFetch(baseUrl, `/jobs/${jobId}`);
}

export function downloadUrl(baseUrl, jobId) {
  return `${normalizeBaseUrl(baseUrl)}/jobs/${jobId}/download`;
}

export function jobOutputsUrl(baseUrl, jobId) {
  return `${normalizeBaseUrl(baseUrl)}/jobs/${jobId}/outputs`;
}

export async function createDistributedRenderGroup(
  baseUrl,
  machineIds = null,
  filename = null,
  fileSizeBytes = null,
  sourceAssetId = null
) {
  const body = {};
  if (Array.isArray(machineIds) && machineIds.length > 0) {
    body.machine_ids = machineIds;
  }
  if (sourceAssetId) {
    body.source_asset_id = sourceAssetId;
  } else {
    body.filename = filename;
    body.file_size_bytes = fileSizeBytes;
  }
  return apiFetch(baseUrl, "/render-groups/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function uploadDistributedRenderInput(baseUrl, groupId, file, onProgress, signal = null) {
  await uploadMultipartInput(baseUrl, "render-groups", groupId, file, onProgress, signal);
}

export async function confirmDistributedJob(
  baseUrl,
  groupId,
  machineIds = null,
  frameRange = null,
  renderOverrides = null,
  scheduling = null,
  analysisSnapshot = null,
  signal = null
) {
  const body = {};
  if (Array.isArray(machineIds) && machineIds.length > 0) {
    body.machine_ids = machineIds;
  }
  if (frameRange) {
    body.frame_start = frameRange.frame_start;
    body.frame_end = frameRange.frame_end;
    body.frame_step = frameRange.frame_step || 1;
  }
  if (renderOverrides) body.render_overrides = renderOverrides;
  if (scheduling) body.scheduling = scheduling;
  if (analysisSnapshot) body.analysis_snapshot = analysisSnapshot;

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
  return `${normalizeBaseUrl(baseUrl)}/render-groups/${groupId}/download`;
}

export function renderGroupOutputsUrl(baseUrl, groupId) {
  return `${normalizeBaseUrl(baseUrl)}/render-groups/${groupId}/outputs`;
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

export async function rerenderGroup(baseUrl, groupId, { frameStart, frameEnd, frameStep = 1, camera = null, renderOverrides = null } = {}) {
  const body = {
    frame_start: frameStart,
    frame_end: frameEnd,
    frame_step: frameStep,
  };
  if (camera) body.camera = camera;
  if (renderOverrides) body.render_overrides = renderOverrides;
  return apiFetch(baseUrl, `/render-groups/${groupId}/rerender`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getVastInstances(baseUrl) {
  return apiFetch(baseUrl, "/vast/instances");
}

export async function getModalInstances(baseUrl) {
  return apiFetch(baseUrl, "/modal/instances");
}
