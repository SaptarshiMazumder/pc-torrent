import { useState, useEffect, useRef, useCallback } from "react";
import { listRenderGroups, getRenderGroup, deleteRenderGroup } from "../services/api";

const POLL_INTERVAL = 3000;
const PAGE_SIZE = 4;

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
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const backendUrlRef = useRef(backendUrl);
  const jobsRef = useRef(jobs);
  const offsetRef = useRef(0);
  // Holds the AbortController for the current fetch (first-page or loadMore).
  // Each new fetch aborts the previous controller so a stale response can't
  // arrive and clobber fresher state (e.g. refresh during in-flight loadMore).
  const controllerRef = useRef(null);
  const loadingMoreRef = useRef(false);

  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  useEffect(() => {
    backendUrlRef.current = backendUrl;
  }, [backendUrl]);

  const appendPage = useCallback((incoming) => {
    if (incoming.length === 0) return;
    setJobs((prev) => {
      const seen = new Set(prev.map((j) => j.group_id));
      const merged = prev.slice();
      for (const g of incoming) {
        if (!seen.has(g.group_id)) merged.push(g);
      }
      return merged;
    });
  }, []);

  const fetchFirstPage = useCallback(async (url) => {
    if (!url) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setLoadingMore(false);
    loadingMoreRef.current = false;
    try {
      const data = await listRenderGroups(url, {
        limit: PAGE_SIZE, offset: 0, signal: controller.signal,
      });
      const groups = normalizeGroups(data?.groups || []);
      setJobs(groups);
      offsetRef.current = groups.length;
      setHasMore(!!data?.has_more);
    } catch (e) {
      if (e?.name !== "AbortError") throw e;
    } finally {
      if (controllerRef.current === controller) setLoading(false);
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMoreRef.current) return;
    const url = backendUrlRef.current;
    if (!url) return;
    loadingMoreRef.current = true;
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoadingMore(true);
    try {
      const data = await listRenderGroups(url, {
        limit: PAGE_SIZE, offset: offsetRef.current, signal: controller.signal,
      });
      const incoming = normalizeGroups(data?.groups || []);
      appendPage(incoming);
      offsetRef.current += incoming.length;
      setHasMore(!!data?.has_more);
    } catch (e) {
      if (e?.name !== "AbortError") throw e;
    } finally {
      loadingMoreRef.current = false;
      if (controllerRef.current === controller) setLoadingMore(false);
    }
  }, [appendPage]);

  useEffect(() => {
    if (backendUrl) fetchFirstPage(backendUrl);
    return () => {
      controllerRef.current?.abort();
    };
  }, [backendUrl, fetchFirstPage]);

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
    // Optimistic: yank the row out of local state synchronously so the
    // card disappears the instant the user clicks delete.  The server
    // DELETE can take its time -- on rejection we restore the row at
    // its original index and re-throw so the caller's error toast fires.
    let snapshot = null;
    setJobs((prev) => {
      const idx = prev.findIndex((j) => j.group_id === id);
      if (idx === -1) return prev;
      snapshot = { row: prev[idx], index: idx };
      return prev.slice(0, idx).concat(prev.slice(idx + 1));
    });
    try {
      await deleteRenderGroup(url, id);
    } catch (err) {
      if (snapshot) {
        setJobs((prev) => {
          if (prev.some((j) => j.group_id === id)) return prev;
          const next = prev.slice();
          next.splice(snapshot.index, 0, snapshot.row);
          return next;
        });
      }
      throw err;
    }
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

  // Patch a single group's row from outside the hook.  Used by the
  // detail-page refresh button so it can fetch one group via
  // /render-groups/{id} and update just that row, instead of paying for
  // a full paginated list re-fetch.
  const updateGroup = useCallback((groupId, data) => {
    if (!groupId || !data) return;
    const updated = normalizeRenderGroup(data);
    if (!updated) return;
    setJobs((prev) =>
      prev.map((job) => (job.group_id === groupId ? { ...job, ...updated } : job))
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

  const refresh = useCallback(() => fetchFirstPage(backendUrlRef.current), [fetchFirstPage]);

  return {
    jobs,
    loading,
    loadingMore,
    hasMore,
    loadMore,
    addRenderGroup,
    removeJob,
    markRenderGroupCancelled,
    updateGroup,
    refresh,
  };
}
