import { useState, useEffect, useRef, useCallback } from "react";
import { getJob } from "../lib/api";

const STORAGE_KEY = "pcrent_jobs";
const POLL_INTERVAL = 3000;

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
      },
      ...prev,
    ]);
  }, []);

  const removeJob = useCallback((jobId) => {
    setJobs((prev) => prev.filter((j) => j.job_id !== jobId));
  }, []);

  // Poll active jobs
  useEffect(() => {
    const poll = async () => {
      const url = backendUrlRef.current;
      if (!url) return;

      setJobs((prev) => {
        const activeJobs = prev.filter(
          (j) => j.status === "pending" || j.status === "running"
        );
        if (activeJobs.length === 0) return prev;

        // Fire off fetches and update state when they resolve
        activeJobs.forEach(async (job) => {
          try {
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
                    }
                  : j
              )
            );
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

  return { jobs, addJob, removeJob };
}
