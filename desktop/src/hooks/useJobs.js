import { useState, useEffect, useRef, useCallback } from "react";
import { listJobs, listRenderGroups, getJob, getRenderGroup } from "../lib/api";

const POLL_INTERVAL = 3000;

export function useJobs(backendUrl) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(false);
  const backendUrlRef = useRef(backendUrl);

  useEffect(() => {
    backendUrlRef.current = backendUrl;
  }, [backendUrl]);

  // Fetch all jobs from server
  const fetchAll = useCallback(async (url) => {
    if (!url) return;
    setLoading(true);
    try {
      const [groups, singleJobs] = await Promise.all([
        listRenderGroups(url).catch(() => []),
        listJobs(url).catch(() => []),
      ]);
      const allJobs = [
        ...groups.map((g) => ({ ...g, group_id: g.id })),
        ...singleJobs.map((j) => ({ ...j, job_id: j.job_id || j.id })),
      ].sort((a, b) => new Date(b.submitted_at) - new Date(a.submitted_at));
      setJobs(allJobs);
    } finally {
      setLoading(false);
    }
  }, []);

  // Load on mount and when backendUrl changes
  useEffect(() => {
    if (backendUrl) fetchAll(backendUrl);
  }, [backendUrl, fetchAll]);

  // Optimistically add a render group after submit (server fetch will reconcile)
  const addRenderGroup = useCallback((groupId, filename, tasks, totalFrames) => {
    setJobs((prev) => [
      {
        group_id: groupId,
        id: groupId,
        filename,
        status: "pending",
        submitted_at: new Date().toISOString(),
        completed_at: null,
        error: null,
        total_frames: totalFrames,
        overall_rendered_frames: 0,
        overall_progress_pct: null,
        tasks: tasks || [],
      },
      ...prev,
    ]);
  }, []);

  // Optimistically add a single job
  const addJob = useCallback((jobId, machineGpu, filename) => {
    setJobs((prev) => [
      {
        job_id: jobId,
        machine_gpu: machineGpu,
        filename,
        status: "pending",
        submitted_at: new Date().toISOString(),
        completed_at: null,
        output_files: [],
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
          ? { ...job, status: "cancelled", completed_at: new Date().toISOString(), error: "Cancelled by user" }
          : job
      )
    );
  }, []);

  // Poll active jobs
  useEffect(() => {
    const poll = async () => {
      const url = backendUrlRef.current;
      if (!url) return;

      setJobs((prev) => {
        const active = prev.filter((j) => !["done", "failed", "cancelled"].includes(j.status));
        if (active.length === 0) return prev;

        active.forEach(async (job) => {
          try {
            if (job.group_id) {
              const updated = await getRenderGroup(url, job.group_id);
              setJobs((cur) =>
                cur.map((j) =>
                  j.group_id === job.group_id
                    ? {
                        ...j,
                        status: updated.status,
                        completed_at: updated.completed_at,
                        error: updated.error,
                        total_frames: typeof updated.total_frames === "number" ? updated.total_frames : null,
                        overall_rendered_frames: typeof updated.overall_rendered_frames === "number" ? updated.overall_rendered_frames : 0,
                        overall_progress_pct: typeof updated.overall_progress_pct === "number" ? updated.overall_progress_pct : null,
                        tasks: updated.tasks?.length > 0 ? updated.tasks : j.tasks,
                      }
                    : j
                )
              );
            } else {
              const updated = await getJob(url, job.job_id);
              setJobs((cur) =>
                cur.map((j) =>
                  j.job_id === job.job_id
                    ? {
                        ...j,
                        status: updated.status,
                        completed_at: updated.completed_at,
                        output_files: updated.output_files || [],
                        error: updated.error,
                        total_frames: typeof updated.total_frames === "number" ? updated.total_frames : null,
                        rendered_frames: typeof updated.rendered_frames === "number" ? updated.rendered_frames : 0,
                        progress_pct: typeof updated.progress_pct === "number" ? updated.progress_pct : null,
                      }
                    : j
                )
              );
            }
          } catch {}
        });

        return prev;
      });
    };

    const id = setInterval(poll, POLL_INTERVAL);
    return () => clearInterval(id);
  }, []);

  return { jobs, loading, addJob, addRenderGroup, removeJob, markRenderGroupCancelled };
}
