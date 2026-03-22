const BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export async function getMachines() {
  const r = await fetch(`${BASE}/machines`);
  return r.json();
}

export async function submitJob(machineId, file) {
  const form = new FormData();
  form.append("machine_id", machineId);
  form.append("blender_file", file);
  const r = await fetch(`${BASE}/jobs`, { method: "POST", body: form });
  if (!r.ok) {
    const err = await r.json();
    throw new Error(err.error || "Failed to submit job");
  }
  return r.json();
}

export async function getJob(jobId) {
  const r = await fetch(`${BASE}/jobs/${jobId}`);
  return r.json();
}

export function downloadUrl(jobId) {
  return `${BASE}/jobs/${jobId}/download`;
}
