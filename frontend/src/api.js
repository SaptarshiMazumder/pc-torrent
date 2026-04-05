const BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

function isMultipartEndpointMissing(error) {
  const message = String(error?.message || "").toLowerCase();
  return (
    message.includes("404") &&
    message.includes("multipart-upload")
  );
}

async function parseJsonError(response, fallbackMessage) {
  try {
    const payload = await response.json();
    if (payload?.detail) return payload.detail;
  } catch {
    // ignore parse errors
  }
  return fallbackMessage;
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
      if (signal && abortListener) {
        signal.removeEventListener("abort", abortListener);
      }
    };

    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
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
  await new Promise((resolve, reject) => {
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
      if (uploaded < blob.size && onBytes) {
        onBytes(blob.size - uploaded);
      }
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

async function postJson(url, body, signal = null) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const detail = await parseJsonError(response, `Request failed (${response.status})`);
    throw new Error(detail);
  }
  return response.json();
}

async function uploadMultipartInput(kind, id, file, onProgress, signal = null) {
  const init = await postJson(
    `${BASE}/${kind}/${id}/multipart-upload/init`,
    {
      file_size_bytes: file.size,
      content_type: "application/octet-stream",
    },
    signal
  );

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
        for (
          let n = partNumber;
          n <= totalParts && n < partNumber + partUrlBatchSize;
          n += 1
        ) {
          batch.push(n);
        }
        const partUrlResp = await postJson(
          `${BASE}/${kind}/${id}/multipart-upload/part-urls`,
          {
            upload_id: init.upload_id,
            part_numbers: batch,
          },
          signal
        );
        const entries = Object.entries(partUrlResp.urls || {});
        for (const [k, v] of entries) {
          const numeric = Number.parseInt(k, 10);
          if (Number.isInteger(numeric) && typeof v === "string") {
            partUrlCache.set(numeric, v);
          }
        }
      }

      const partUrl = partUrlCache.get(partNumber);
      if (!partUrl) {
        throw new Error(`Missing upload URL for part ${partNumber}`);
      }
      partUrlCache.delete(partNumber);

      const start = (partNumber - 1) * partSize;
      const end = Math.min(file.size, start + partSize);
      const blob = file.slice(start, end);
      const etag = await uploadBlobPart(partUrl, blob, signal, (delta) => {
        uploadedBytes += delta;
        reportProgress();
      });
      completedParts.push({
        part_number: partNumber,
        etag,
      });
    }

    await postJson(
      `${BASE}/${kind}/${id}/multipart-upload/complete`,
      {
        upload_id: init.upload_id,
        parts: completedParts,
      },
      signal
    );
    if (onProgress) onProgress(100);
  } catch (error) {
    await postJson(
      `${BASE}/${kind}/${id}/multipart-upload/abort`,
      { upload_id: init.upload_id },
      null
    ).catch(() => {});
    throw error;
  }
}

export async function getMachines() {
  const r = await fetch(`${BASE}/machines`);
  return r.json();
}

export async function submitJob(machineId, file, onProgress, signal = null) {
  const reqRes = await fetch(`${BASE}/jobs/request-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      machine_id: machineId,
      filename: file.name,
      file_size_bytes: file.size,
    }),
    signal,
  });
  if (!reqRes.ok) {
    const detail = await parseJsonError(reqRes, "Failed to request upload URL");
    throw new Error(detail);
  }
  const requestInfo = await reqRes.json();
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

  const confirmRes = await fetch(`${BASE}/jobs/${jobId}/confirm-upload`, {
    method: "POST",
    signal,
  });
  if (!confirmRes.ok) {
    const detail = await parseJsonError(confirmRes, "Failed to confirm upload");
    throw new Error(detail);
  }

  return { job_id: jobId, status: "pending" };
}

export async function getJob(jobId) {
  const r = await fetch(`${BASE}/jobs/${jobId}`);
  return r.json();
}

export function downloadUrl(jobId) {
  return `${BASE}/jobs/${jobId}/download`;
}

export async function createDistributedRenderGroup(machineIds, filename, fileSizeBytes = null) {
  const createRes = await fetch(`${BASE}/render-groups/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      machine_ids: machineIds,
      filename,
      file_size_bytes: fileSizeBytes,
    }),
  });
  if (!createRes.ok) {
    const detail = await parseJsonError(createRes, "Failed to create render group");
    throw new Error(detail);
  }
  return createRes.json();
}

export async function uploadDistributedRenderInput(groupId, file, onProgress, signal = null) {
  await uploadMultipartInput("render-groups", groupId, file, onProgress, signal);
}

export async function confirmDistributedJob(groupId, machineIds, frameRange = null) {
  const body = { machine_ids: machineIds };
  if (frameRange) {
    body.frame_start = frameRange.frame_start;
    body.frame_end = frameRange.frame_end;
    body.frame_step = frameRange.frame_step || 1;
  }
  const confirmRes = await fetch(`${BASE}/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!confirmRes.ok) {
    const detail = await parseJsonError(confirmRes, "Failed to confirm upload");
    throw new Error(detail);
  }
  return confirmRes.json();
}

export async function getRenderGroup(groupId) {
  const r = await fetch(`${BASE}/render-groups/${groupId}`);
  return r.json();
}

export function renderGroupDownloadUrl(groupId) {
  return `${BASE}/render-groups/${groupId}/download`;
}

export function logsStreamUrl() {
  return `${BASE}/logs/stream`;
}
