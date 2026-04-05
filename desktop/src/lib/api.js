export async function getMachines(baseUrl) {
  const r = await fetch(`${baseUrl}/machines`);
  if (!r.ok) throw new Error("Failed to fetch machines");
  return r.json();
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
  // Step 1: Get presigned upload URL
  const reqRes = await fetch(`${baseUrl}/jobs/request-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_id: machineId, filename: file.name }),
  });
  if (!reqRes.ok) {
    const err = await reqRes.json();
    throw new Error(err.detail || "Failed to request upload URL");
  }
  const { job_id, upload_url } = await reqRes.json();

  // Step 2: Upload file directly to R2 via presigned URL
  await uploadFileToPresignedUrl(upload_url, file, onProgress);

  // Step 3: Confirm upload
  const confirmRes = await fetch(`${baseUrl}/jobs/${job_id}/confirm-upload`, {
    method: "POST",
  });
  if (!confirmRes.ok) {
    const err = await confirmRes.json();
    throw new Error(err.detail || "Failed to confirm upload");
  }

  return { job_id, status: "pending" };
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

// ---- Distributed Rendering (Render Groups) ----

export async function createDistributedRenderGroup(baseUrl, machineIds, filename) {
  const createRes = await fetch(`${baseUrl}/render-groups/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_ids: machineIds, filename }),
  });
  if (!createRes.ok) {
    const err = await createRes.json();
    throw new Error(err.detail || "Failed to create render group");
  }
  return createRes.json();
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
  const confirmRes = await fetch(`${baseUrl}/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!confirmRes.ok) {
    const err = await confirmRes.json();
    throw new Error(err.detail || "Failed to confirm upload");
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
