import { useState, useEffect, useRef, useCallback } from "react";
import { listRenderGroups, getRenderGroup, deleteRenderGroup } from "../services/api";

const POLL_INTERVAL = 3000;

function toTimestamp(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
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

function normalizeGroups(groups) {
  if (!Array.isArray(groups)) return [];
  return groups
    .map(normalizeRenderGroup)
    .filter(Boolean)
    .sort((a, b) => toTimestamp(b.submitted_at) - toTimestamp(a.submitted_at));
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

  const fetchAll = useCallback(async (url) => {
    if (!url) return;
    setLoading(true);
    try {
      const groups = await listRenderGroups(url).catch(() => []);
      setJobs(normalizeGroups(groups));
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

  const removeJob = useCallback(async (id) => {
    const url = backendUrlRef.current;
    try {
      await deleteRenderGroup(url, id);
    } catch {
      // If the backend rejects (e.g. job still active), don't remove from UI
      return;
    }
    setJobs((prev) => prev.filter((j) => j.group_id !== id));
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

  // Poll active groups
  useEffect(() => {
    let polling = false;

    const poll = async () => {
      if (polling) return;
      const url = backendUrlRef.current;
      if (!url) return;

      const active = jobsRef.current.filter(
        (j) => j.group_id && !["done", "failed", "cancelled"].includes(j.status)
      );
      if (active.length === 0) return;

      polling = true;
      try {
        await Promise.all(
          active.map(async (job) => {
            try {
              const updated = normalizeRenderGroup(await getRenderGroup(url, job.group_id));
              if (!updated) return;
              setJobs((cur) =>
                cur.map((existing) =>
                  existing.group_id === job.group_id
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

  return { jobs, loading, addRenderGroup, removeJob, markRenderGroupCancelled, refresh };
}
