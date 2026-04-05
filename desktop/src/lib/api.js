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

async function uploadMultipartInput(baseUrl, kind, id, file, onProgress, signal = null) {
  const init = await postJson(
    `${baseUrl}/${kind}/${id}/multipart-upload/init`,
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
          `${baseUrl}/${kind}/${id}/multipart-upload/part-urls`,
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
      `${baseUrl}/${kind}/${id}/multipart-upload/complete`,
      {
        upload_id: init.upload_id,
        parts: completedParts,
      },
      signal
    );
    if (onProgress) onProgress(100);
  } catch (error) {
    await postJson(
      `${baseUrl}/${kind}/${id}/multipart-upload/abort`,
      { upload_id: init.upload_id },
      null
    ).catch(() => {});
    throw error;
  }
}

export async function getMachines(baseUrl) {
  const r = await fetch(`${baseUrl}/machines`);
  if (!r.ok) throw new Error("Failed to fetch machines");
  return r.json();
}

export async function submitJob(baseUrl, machineId, file, onProgress, signal = null) {
  const reqRes = await fetch(`${baseUrl}/jobs/request-upload`, {
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

  const confirmRes = await fetch(`${baseUrl}/jobs/${jobId}/confirm-upload`, {
    method: "POST",
    signal,
  });
  if (!confirmRes.ok) {
    const detail = await parseJsonError(confirmRes, "Failed to confirm upload");
    throw new Error(detail);
  }

  return { job_id: jobId, status: "pending" };
}

export async function getJob(baseUrl, jobId) {
  const r = await fetch(`${baseUrl}/jobs/${jobId}`);
  if (!r.ok) throw new Error("Failed to fetch job");
  return r.json();
}

export function downloadUrl(baseUrl, jobId) {
  return `${baseUrl}/jobs/${jobId}/download`;
}

export function jobOutputsUrl(baseUrl, jobId) {
  return `${baseUrl}/jobs/${jobId}/outputs`;
}

export async function createDistributedRenderGroup(baseUrl, machineIds, filename, fileSizeBytes = null) {
  const createRes = await fetch(`${baseUrl}/render-groups/create`, {
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

export async function uploadDistributedRenderInput(baseUrl, groupId, file, onProgress, signal = null) {
  await uploadMultipartInput(baseUrl, "render-groups", groupId, file, onProgress, signal);
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
  const confirmRes = await fetch(`${baseUrl}/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!confirmRes.ok) {
    const detail = await parseJsonError(confirmRes, "Failed to confirm upload");
    throw new Error(detail);
  }
  return confirmRes.json();
}

export async function getRenderGroup(baseUrl, groupId) {
  const r = await fetch(`${baseUrl}/render-groups/${groupId}`);
  if (!r.ok) throw new Error("Failed to fetch render group");
  return r.json();
}

export function renderGroupDownloadUrl(baseUrl, groupId) {
  return `${baseUrl}/render-groups/${groupId}/download`;
}

export function renderGroupOutputsUrl(baseUrl, groupId) {
  return `${baseUrl}/render-groups/${groupId}/outputs`;
}

export async function cancelRenderGroup(baseUrl, groupId) {
  const response = await fetch(`${baseUrl}/render-groups/${groupId}/cancel`, {
    method: "POST",
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to cancel render group");
  }
  return response.json();
}
