import { useState, useEffect, useRef, useCallback } from "react";
import { listJobs, listRenderGroups, getJob, getRenderGroup } from "../lib/api";

const POLL_INTERVAL = 3000;

function toTimestamp(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function canonicalJobKey(job) {
  if (job?.group_id) return `group:${job.group_id}`;
  if (job?.job_id) return `job:${job.job_id}`;
  return "";
}

function normalizeRenderGroup(group) {
  const groupId =
    typeof group?.group_id === "string" && group.group_id
      ? group.group_id
      : typeof group?.id === "string" && group.id
      ? group.id
      : "";
  if (!groupId) return null;

  const filename =
    (typeof group?.input_filename === "string" && group.input_filename.trim()) ||
    (typeof group?.filename === "string" && group.filename.trim()) ||
    "Untitled";

  return {
    ...group,
    group_id: groupId,
    id: groupId,
    filename,
    input_filename: filename,
    status: group?.status || "pending",
    tasks: Array.isArray(group?.tasks) ? group.tasks : [],
    overall_progress_pct:
      typeof group?.overall_progress_pct === "number" ? group.overall_progress_pct : null,
    overall_rendered_frames:
      typeof group?.overall_rendered_frames === "number" ? group.overall_rendered_frames : 0,
    available_output_files_count:
      typeof group?.available_output_files_count === "number" ? group.available_output_files_count : 0,
    latest_output_file: group?.latest_output_file || null,
  };
}

function normalizeSingleJob(job) {
  const jobId =
    typeof job?.job_id === "string" && job.job_id
      ? job.job_id
      : typeof job?.id === "string" && job.id
      ? job.id
      : "";
  if (!jobId) return null;

  const filename =
    (typeof job?.input_filename === "string" && job.input_filename.trim()) ||
    (typeof job?.filename === "string" && job.filename.trim()) ||
    "Untitled";

  return {
    ...job,
    job_id: jobId,
    id: jobId,
    filename,
    input_filename: filename,
    status: job?.status || "pending",
    output_files: Array.isArray(job?.output_files) ? job.output_files : [],
    progress_pct: typeof job?.progress_pct === "number" ? job.progress_pct : null,
    rendered_frames: typeof job?.rendered_frames === "number" ? job.rendered_frames : 0,
    output_files_count:
      typeof job?.output_files_count === "number"
        ? job.output_files_count
        : Array.isArray(job?.output_files)
        ? job.output_files.length
        : 0,
  };
}

function mergeAndNormalizeJobs(groups, singleJobs) {
  const combined = [
    ...(Array.isArray(groups) ? groups.map(normalizeRenderGroup).filter(Boolean) : []),
    ...(Array.isArray(singleJobs) ? singleJobs.map(normalizeSingleJob).filter(Boolean) : []),
  ].sort((a, b) => toTimestamp(b.submitted_at) - toTimestamp(a.submitted_at));

  const seen = new Set();
  const deduped = [];
  for (const job of combined) {
    const key = canonicalJobKey(job);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    deduped.push(job);
  }
  return deduped;
}

export function useJobs(backendUrl) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const backendUrlRef = useRef(backendUrl);
  const jobsRef = useRef(jobs);

  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  useEffect(() => {
    backendUrlRef.current = backendUrl;
  }, [backendUrl]);

  // Fetch full renter history: render-groups + legacy single jobs.
  const fetchAll = useCallback(async (url) => {
    if (!url) return;
    setLoading(true);
    try {
      const [groups, singleJobs] = await Promise.all([
        listRenderGroups(url).catch(() => []),
        listJobs(url).catch(() => []),
      ]);
      setJobs(mergeAndNormalizeJobs(groups, singleJobs));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (backendUrl) fetchAll(backendUrl);
  }, [backendUrl, fetchAll]);

  const addRenderGroup = useCallback((groupId, filename, tasks, totalFrames) => {
    setJobs((prev) => [
      {
        group_id: groupId,
        id: groupId,
        filename: filename || "Untitled",
        input_filename: filename || "Untitled",
        status: "pending",
        submitted_at: new Date().toISOString(),
        completed_at: null,
        error: null,
        total_frames: totalFrames,
        overall_rendered_frames: 0,
        overall_progress_pct: null,
        available_output_files_count: 0,
        latest_output_file: null,
        tasks: Array.isArray(tasks) ? tasks : [],
      },
      ...prev,
    ]);
  }, []);

  const addJob = useCallback((jobId, machineGpu, filename) => {
    setJobs((prev) => [
      {
        job_id: jobId,
        id: jobId,
        machine_gpu: machineGpu,
        filename: filename || "Untitled",
        input_filename: filename || "Untitled",
        status: "pending",
        submitted_at: new Date().toISOString(),
        completed_at: null,
        output_files: [],
        output_files_count: 0,
        latest_output_file: null,
        error: null,
        total_frames: null,
        rendered_frames: 0,
        progress_pct: null,
      },
      ...prev,
    ]);
  }, []);

  const removeJob = useCallback((id) => {
    setJobs((prev) => prev.filter((j) => (j.group_id || j.job_id) !== id));
  }, []);

  const markRenderGroupCancelled = useCallback((groupId) => {
    setJobs((prev) =>
      prev.map((job) =>
        job.group_id === groupId
          ? {
              ...job,
              status: "cancelled",
              completed_at: new Date().toISOString(),
              error: "Cancelled by user",
            }
          : job
      )
    );
  }, []);

  // Poll active jobs
  useEffect(() => {
    let polling = false;

    const poll = async () => {
      if (polling) return;
      const url = backendUrlRef.current;
      if (!url) return;

      const current = jobsRef.current;
      const active = current.filter(
        (j) => canonicalJobKey(j) && !["done", "failed", "cancelled"].includes(j.status)
      );
      if (active.length === 0) return;

      polling = true;
      try {
        await Promise.all(
          active.map(async (job) => {
            try {
              if (job.group_id) {
                const updated = normalizeRenderGroup(
                  await getRenderGroup(url, job.group_id)
                );
                if (!updated) return;

                setJobs((cur) =>
                  cur.map((existing) =>
                    canonicalJobKey(existing) === `group:${job.group_id}`
                      ? {
                          ...existing,
                          ...updated,
                          tasks:
                            updated.tasks?.length > 0
                              ? updated.tasks
                              : Array.isArray(existing.tasks)
                              ? existing.tasks
                              : [],
                        }
                      : existing
                  )
                );
                return;
              }

              if (!job.job_id) return;
              const updated = normalizeSingleJob(await getJob(url, job.job_id));
              if (!updated) return;

              setJobs((cur) =>
                cur.map((existing) =>
                  canonicalJobKey(existing) === `job:${job.job_id}`
                    ? { ...existing, ...updated }
                    : existing
                )
              );
            } catch {
              // Polling failures should not break history rendering.
            }
          })
        );
      } finally {
        polling = false;
      }
    };

    const id = setInterval(poll, POLL_INTERVAL);
    return () => clearInterval(id);
  }, []);

  const refresh = useCallback(() => fetchAll(backendUrlRef.current), [fetchAll]);

  return { jobs, loading, addJob, addRenderGroup, removeJob, markRenderGroupCancelled, refresh };
}
