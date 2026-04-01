export async function getMachines(baseUrl) {
  const r = await fetch(`${baseUrl}/machines`);
  if (!r.ok) throw new Error("Failed to fetch machines");
  return r.json();
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
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", upload_url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`Upload failed with status ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(file);
  });

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

// ---- Distributed Rendering (Render Groups) ----

export async function submitDistributedJob(baseUrl, machineIds, file, onProgress, frameRange = null) {
  // Step 1: Create render group
  const createRes = await fetch(`${baseUrl}/render-groups/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ machine_ids: machineIds, filename: file.name }),
  });
  if (!createRes.ok) {
    const err = await createRes.json();
    throw new Error(err.detail || "Failed to create render group");
  }
  const { group_id, upload_url } = await createRes.json();

  // Step 2: Upload file to R2
  await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", upload_url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      };
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`Upload failed with status ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(file);
  });

  // Step 3: Confirm upload + distribute frames (pass client-side frame range if available)
  if (onProgress) onProgress(100);
  return confirmDistributedJob(baseUrl, group_id, machineIds, frameRange);
}

export async function confirmDistributedJob(baseUrl, groupId, machineIds, frameRange = null) {
  const body = { machine_ids: machineIds };
  if (frameRange) {
    body.frame_start = frameRange.frame_start;
    body.frame_end = frameRange.frame_end;
    body.frame_step = frameRange.frame_step || 1;
  }
  const confirmRes = await fetch(`${baseUrl}/render-groups/${groupId}/confirm-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
