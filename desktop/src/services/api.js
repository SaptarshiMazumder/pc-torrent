import { auth } from "../firebase/config";

function normalizeBaseUrl(baseUrl) {
  return String(baseUrl || "").trim().replace(/\/+$/, "");
}

async function authHeaders(forceRefresh = false) {
  try {
    const token = await auth.currentUser?.getIdToken(forceRefresh);
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

async function apiFetch(baseUrl, path, options = {}, _retry = false) {
  const normalizedBase = normalizeBaseUrl(baseUrl);
  if (!normalizedBase) {
    throw new Error("Backend URL is not configured");
  }

  const headers = {
    ...(options.headers || {}),
    ...(await authHeaders(_retry)),
  };

  let response;
  try {
    response = await fetch(`${normalizedBase}${path}`, { ...options, headers });
  } catch (error) {
    // Preserve AbortError so callers can distinguish "I cancelled this"
    // from a genuine network failure.
    if (error?.name === "AbortError") throw error;
    const detail = error?.message || String(error);
    throw new Error(`Network error reaching ${normalizedBase}${path}: ${detail}`);
  }

  // On first 401, force-refresh the Firebase token and retry once.
  if (response.status === 401 && !_retry) {
    return apiFetch(baseUrl, path, options, true);
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

export async function getAvailableMachines(baseUrl) {
  return apiFetch(baseUrl, "/machines/available");
}

export async function getAdminConfig(baseUrl) {
  return apiFetch(baseUrl, "/admin/config");
}

export async function putAdminConfig(baseUrl, configDict) {
  return apiFetch(baseUrl, "/admin/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: configDict }),
  });
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

export async function listRenderGroups(baseUrl, { limit = 5, offset = 0, signal } = {}) {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiFetch(baseUrl, `/render-groups?${qs.toString()}`, { signal });
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
  tier = null,
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
  if (tier) body.tier = tier;

  return apiFetch(baseUrl, `/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function estimateRenderGroup(
  baseUrl,
  {
    analysisSnapshot = null,
    renderOverrides = null,
    frameStart = null,
    frameEnd = null,
    frameStep = null,
    machineIds = null,
    fileSizeBytes = null,
  } = {},
) {
  // Stateless RPC — frontend has both the analyzer snapshot (desktop
  // Blender ran analysis at .blend pick time) and the user's edited
  // overrides locally; the backend merges and returns cost / wall-time
  // estimates without touching the database.
  const body = {
    analysis_snapshot: analysisSnapshot,
    render_overrides: renderOverrides,
    frame_start: frameStart,
    frame_end: frameEnd,
    frame_step: frameStep,
    machine_ids: machineIds,
    file_size_bytes: fileSizeBytes,
  };
  return apiFetch(baseUrl, `/pre-render/estimate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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

export async function getRenderGroupPendingQueue(baseUrl, groupId, { signal } = {}) {
  return apiFetch(baseUrl, `/render-groups/${groupId}/pending-queue`, { signal });
}

export async function cancelRenderGroup(baseUrl, groupId) {
  return apiFetch(baseUrl, `/render-groups/${groupId}/cancel`, { method: "POST" });
}

export async function cancelAllRenderGroups(baseUrl) {
  return apiFetch(baseUrl, "/render-groups/cancel-all", { method: "POST" });
}

export async function deleteRenderGroup(baseUrl, groupId) {
  return apiFetch(baseUrl, `/render-groups/${groupId}`, { method: "DELETE" });
}

export async function retryJobChunk(baseUrl, jobId) {
  return apiFetch(baseUrl, `/jobs/${jobId}/retry`, { method: "POST" });
}

export async function cancelJob(baseUrl, jobId) {
  return apiFetch(baseUrl, `/jobs/${jobId}/cancel`, { method: "POST" });
}

export async function getAllowedStallTimes(baseUrl, jobId) {
  // Returns the dispatch-time-resolved kill-time deadline dict for a job.
  // 404 for legacy rows pre-dating the column or missing jobs; we map
  // that to null so callers can render "not yet known" gracefully.
  try {
    return await apiFetch(baseUrl, `/jobs/${jobId}/allowed-stall-times`);
  } catch (e) {
    if (e?.status === 404) return null;
    throw e;
  }
}

export async function getVastInstances(baseUrl) {
  return apiFetch(baseUrl, "/vast/instances");
}

export async function getModalInstances(baseUrl) {
  return apiFetch(baseUrl, "/modal/instances");
}
