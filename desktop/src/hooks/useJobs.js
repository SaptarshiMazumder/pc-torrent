import { useState, useEffect, useRef, useCallback } from "react";
import { getJob, getRenderGroup } from "../lib/api";

const STORAGE_KEY = "pcrent_jobs";
const POLL_INTERVAL = 5000;

function loadJobs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveJobs(jobs) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(jobs));
}

export function useJobs(backendUrl) {
  const [jobs, setJobs] = useState(loadJobs);
  const backendUrlRef = useRef(backendUrl);

  useEffect(() => {
    backendUrlRef.current = backendUrl;
  }, [backendUrl]);

  useEffect(() => {
    saveJobs(jobs);
  }, [jobs]);

  // Add a single-machine job (legacy)
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

  // Add a distributed render group
  const addRenderGroup = useCallback((groupId, filename, tasks, totalFrames) => {
    setJobs((prev) => [
      {
        group_id: groupId,
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

  const removeJob = useCallback((id) => {
    setJobs((prev) => prev.filter((j) => (j.group_id || j.job_id) !== id));
  }, []);

  // Poll active jobs and render groups
  useEffect(() => {
    const poll = async () => {
      const url = backendUrlRef.current;
      if (!url) return;

      setJobs((prev) => {
        const activeJobs = prev.filter((j) => !["done", "failed", "cancelled"].includes(j.status));
        if (activeJobs.length === 0) return prev;

        activeJobs.forEach(async (job) => {
          try {
            if (job.group_id) {
              // Poll render group
              const updated = await getRenderGroup(url, job.group_id);
              setJobs((current) =>
                current.map((j) =>
                  j.group_id === job.group_id
                    ? {
                        ...j,
                        status: updated.status,
                        completed_at: updated.completed_at,
                        error: updated.error,
                        total_frames:
                          typeof updated.total_frames === "number"
                            ? updated.total_frames
                            : null,
                        overall_rendered_frames:
                          typeof updated.overall_rendered_frames === "number"
                            ? updated.overall_rendered_frames
                            : 0,
                        overall_progress_pct:
                          typeof updated.overall_progress_pct === "number"
                            ? updated.overall_progress_pct
                            : null,
                        tasks: updated.tasks?.length > 0 ? updated.tasks : j.tasks,
                      }
                    : j
                )
              );
            } else {
              // Poll single job (legacy)
              const updated = await getJob(url, job.job_id);
              setJobs((current) =>
                current.map((j) =>
                  j.job_id === job.job_id
                    ? {
                        ...j,
                        status: updated.status,
                        completed_at: updated.completed_at,
                        output_files: updated.output_files || [],
                        error: updated.error,
                        total_frames:
                          typeof updated.total_frames === "number"
                            ? updated.total_frames
                            : null,
                        rendered_frames:
                          typeof updated.rendered_frames === "number"
                            ? updated.rendered_frames
                            : 0,
                        progress_pct:
                          typeof updated.progress_pct === "number"
                            ? updated.progress_pct
                            : null,
                      }
                    : j
                )
              );
            }
          } catch {
            // Silently skip failed polls
          }
        });

        return prev;
      });
    };

    const id = setInterval(poll, POLL_INTERVAL);
    return () => clearInterval(id);
  }, []);

  return { jobs, addJob, addRenderGroup, removeJob };
}
