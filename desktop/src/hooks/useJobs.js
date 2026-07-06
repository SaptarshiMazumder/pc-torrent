import { useState, useEffect, useRef, useCallback } from "react";
import { listRenderGroups, getRenderGroup, deleteRenderGroup } from "../services/api";
import i18n from "../i18n/i18n";

const POLL_INTERVAL = 3000;
const PAGE_SIZE = 4;
const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

// Past renders never change once terminal -- cache them in module scope so
// the first list endpoint hit per session is the only one.  Survives hook
// remount / navigation; cleared only when the app reloads.
let _pastCache = null; // { jobs, offset, hasMore } | null

function isTerminal(status) {
  return TERMINAL_STATUSES.has(status);
}

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
    i18n.t("shared:job.untitled");

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

// Belt-and-braces client-side filter for the section split.  The server
// also filters when `status_group` is sent, but if the deployed server
// is older than the desktop build it simply ignores the unknown query
// param and returns the full list -- which would then dump every row
// into whichever section made the request.  Filtering here keeps the
// UI correct regardless of backend version skew.
function filterByStatusGroup(jobs, statusGroup) {
  if (statusGroup === "active") {
    return jobs.filter((j) => !TERMINAL_STATUSES.has(j.status));
  }
  if (statusGroup === "terminal") {
    return jobs.filter((j) => TERMINAL_STATUSES.has(j.status));
  }
  return jobs;
}

function dedupeAppend(prev, incoming) {
  if (incoming.length === 0) return prev;
  const seen = new Set(prev.map((j) => j.group_id));
  const merged = prev.slice();
  for (const g of incoming) {
    if (!seen.has(g.group_id)) merged.push(g);
  }
  return merged;
}

export function useJobs(backendUrl) {
  const [ongoingJobs, setOngoingJobs] = useState([]);
  const [pastJobs, setPastJobs] = useState(() => _pastCache?.jobs?.slice() || []);
  const [loadingOngoing, setLoadingOngoing] = useState(false);
  const [loadingPast, setLoadingPast] = useState(false);
  const [loadingMoreOngoing, setLoadingMoreOngoing] = useState(false);
  const [loadingMorePast, setLoadingMorePast] = useState(false);
  const [hasMoreOngoing, setHasMoreOngoing] = useState(false);
  const [hasMorePast, setHasMorePast] = useState(_pastCache?.hasMore ?? false);

  const backendUrlRef = useRef(backendUrl);
  const ongoingRef = useRef(ongoingJobs);
  const pastRef = useRef(pastJobs);
  const ongoingOffsetRef = useRef(0);
  const pastOffsetRef = useRef(_pastCache?.offset ?? 0);
  const ongoingControllerRef = useRef(null);
  const pastControllerRef = useRef(null);
  const loadingMoreOngoingRef = useRef(false);
  const loadingMorePastRef = useRef(false);

  useEffect(() => { ongoingRef.current = ongoingJobs; }, [ongoingJobs]);
  useEffect(() => { pastRef.current = pastJobs; }, [pastJobs]);
  useEffect(() => { backendUrlRef.current = backendUrl; }, [backendUrl]);

  // Sync the past cache so a remount within the session restores
  // identical state.  Cache mirrors what the user has seen.
  const writePastCache = useCallback(() => {
    _pastCache = {
      jobs: pastRef.current.slice(),
      offset: pastOffsetRef.current,
      hasMore: hasMorePast,
    };
  }, [hasMorePast]);

  const fetchOngoingFirstPage = useCallback(async (url) => {
    if (!url) return;
    ongoingControllerRef.current?.abort();
    const controller = new AbortController();
    ongoingControllerRef.current = controller;
    setLoadingOngoing(true);
    setLoadingMoreOngoing(false);
    loadingMoreOngoingRef.current = false;
    try {
      const data = await listRenderGroups(url, {
        limit: PAGE_SIZE, offset: 0, statusGroup: "active",
        signal: controller.signal,
      });
      const groups = filterByStatusGroup(normalizeGroups(data?.groups || []), "active");
      setOngoingJobs(groups);
      ongoingOffsetRef.current = (data?.groups?.length) || 0;
      setHasMoreOngoing(!!data?.has_more);
    } catch (e) {
      if (e?.name !== "AbortError") throw e;
    } finally {
      if (ongoingControllerRef.current === controller) setLoadingOngoing(false);
    }
  }, []);

  const fetchPastFirstPage = useCallback(async (url) => {
    if (!url) return;
    pastControllerRef.current?.abort();
    const controller = new AbortController();
    pastControllerRef.current = controller;
    setLoadingPast(true);
    setLoadingMorePast(false);
    loadingMorePastRef.current = false;
    try {
      const data = await listRenderGroups(url, {
        limit: PAGE_SIZE, offset: 0, statusGroup: "terminal",
        signal: controller.signal,
      });
      const groups = filterByStatusGroup(normalizeGroups(data?.groups || []), "terminal");
      setPastJobs(groups);
      pastOffsetRef.current = (data?.groups?.length) || 0;
      setHasMorePast(!!data?.has_more);
      _pastCache = {
        jobs: groups.slice(),
        offset: pastOffsetRef.current,
        hasMore: !!data?.has_more,
      };
    } catch (e) {
      if (e?.name !== "AbortError") throw e;
    } finally {
      if (pastControllerRef.current === controller) setLoadingPast(false);
    }
  }, []);

  const loadMoreOngoing = useCallback(async () => {
    if (loadingMoreOngoingRef.current) return;
    const url = backendUrlRef.current;
    if (!url) return;
    loadingMoreOngoingRef.current = true;
    const controller = new AbortController();
    ongoingControllerRef.current = controller;
    setLoadingMoreOngoing(true);
    try {
      const data = await listRenderGroups(url, {
        limit: PAGE_SIZE, offset: ongoingOffsetRef.current,
        statusGroup: "active", signal: controller.signal,
      });
      const rawCount = (data?.groups?.length) || 0;
      const incoming = filterByStatusGroup(normalizeGroups(data?.groups || []), "active");
      setOngoingJobs((prev) => dedupeAppend(prev, incoming));
      ongoingOffsetRef.current += rawCount;
      setHasMoreOngoing(!!data?.has_more);
    } catch (e) {
      if (e?.name !== "AbortError") throw e;
    } finally {
      loadingMoreOngoingRef.current = false;
      if (ongoingControllerRef.current === controller) setLoadingMoreOngoing(false);
    }
  }, []);

  const loadMorePast = useCallback(async () => {
    if (loadingMorePastRef.current) return;
    const url = backendUrlRef.current;
    if (!url) return;
    loadingMorePastRef.current = true;
    const controller = new AbortController();
    pastControllerRef.current = controller;
    setLoadingMorePast(true);
    try {
      const data = await listRenderGroups(url, {
        limit: PAGE_SIZE, offset: pastOffsetRef.current,
        statusGroup: "terminal", signal: controller.signal,
      });
      const rawCount = (data?.groups?.length) || 0;
      const incoming = filterByStatusGroup(normalizeGroups(data?.groups || []), "terminal");
      setPastJobs((prev) => {
        const next = dedupeAppend(prev, incoming);
        _pastCache = {
          jobs: next.slice(),
          offset: pastOffsetRef.current + rawCount,
          hasMore: !!data?.has_more,
        };
        return next;
      });
      pastOffsetRef.current += rawCount;
      setHasMorePast(!!data?.has_more);
    } catch (e) {
      if (e?.name !== "AbortError") throw e;
    } finally {
      loadingMorePastRef.current = false;
      if (pastControllerRef.current === controller) setLoadingMorePast(false);
    }
  }, []);

  // Initial load -- ongoing always; past only if cache miss this session.
  useEffect(() => {
    if (!backendUrl) return;
    fetchOngoingFirstPage(backendUrl);
    if (!_pastCache) {
      fetchPastFirstPage(backendUrl);
    }
    return () => {
      ongoingControllerRef.current?.abort();
      pastControllerRef.current?.abort();
    };
  }, [backendUrl, fetchOngoingFirstPage, fetchPastFirstPage]);

  // Move a row from ongoing -> past (e.g. polling flips status to terminal).
  const transitionToPast = useCallback((groupId, updatedRow) => {
    let removed = null;
    setOngoingJobs((prev) => {
      const idx = prev.findIndex((j) => j.group_id === groupId);
      if (idx === -1) return prev;
      removed = prev[idx];
      return prev.slice(0, idx).concat(prev.slice(idx + 1));
    });
    if (!removed) return;
    const merged = { ...removed, ...(updatedRow || {}) };
    setPastJobs((prev) => {
      if (prev.some((j) => j.group_id === groupId)) return prev;
      const next = [merged, ...prev];
      _pastCache = {
        jobs: next.slice(),
        offset: (pastOffsetRef.current || 0) + 1,
        hasMore: hasMorePast,
      };
      return next;
    });
    pastOffsetRef.current = (pastOffsetRef.current || 0) + 1;
  }, [hasMorePast]);

  // Move a row from past -> ongoing (e.g. manual retry on a terminal
  // group flips it back to pending/running server-side).  Symmetric to
  // transitionToPast; without this, ``updateGroup`` silently no-ops on
  // past-listed groups because its in-place map runs over ongoingJobs.
  const transitionToOngoing = useCallback((groupId, updatedRow) => {
    let removed = null;
    setPastJobs((prev) => {
      const idx = prev.findIndex((j) => j.group_id === groupId);
      if (idx === -1) return prev;
      removed = prev[idx];
      const next = prev.slice(0, idx).concat(prev.slice(idx + 1));
      _pastCache = {
        jobs: next.slice(),
        offset: Math.max(0, (pastOffsetRef.current || 0) - 1),
        hasMore: hasMorePast,
      };
      return next;
    });
    if (!removed) return;
    pastOffsetRef.current = Math.max(0, (pastOffsetRef.current || 0) - 1);
    const merged = { ...removed, ...(updatedRow || {}) };
    setOngoingJobs((prev) => {
      if (prev.some((j) => j.group_id === groupId)) return prev;
      return [merged, ...prev];
    });
  }, [hasMorePast]);

  const addRenderGroup = useCallback((groupId, filename, tasks, totalFrames) => {
    setOngoingJobs((prev) => [
      {
        group_id: groupId,
        id: groupId,
        filename: filename || i18n.t("shared:job.untitled"),
        input_filename: filename || i18n.t("shared:job.untitled"),
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
    // Optimistic: yank the row out of whichever list contains it.
    let snapshot = null;
    setOngoingJobs((prev) => {
      const idx = prev.findIndex((j) => j.group_id === id);
      if (idx === -1) return prev;
      snapshot = { list: "ongoing", row: prev[idx], index: idx };
      return prev.slice(0, idx).concat(prev.slice(idx + 1));
    });
    if (!snapshot) {
      setPastJobs((prev) => {
        const idx = prev.findIndex((j) => j.group_id === id);
        if (idx === -1) return prev;
        snapshot = { list: "past", row: prev[idx], index: idx };
        const next = prev.slice(0, idx).concat(prev.slice(idx + 1));
        _pastCache = {
          jobs: next.slice(),
          offset: Math.max(0, (pastOffsetRef.current || 0) - 1),
          hasMore: hasMorePast,
        };
        pastOffsetRef.current = Math.max(0, (pastOffsetRef.current || 0) - 1);
        return next;
      });
    }
    try {
      await deleteRenderGroup(url, id);
    } catch (err) {
      // 404 == the group is already gone on the backend, which is exactly
      // the end state Remove wants.  Keep the optimistic removal; don't
      // roll back and don't surface an error.
      if (err?.status === 404) return;
      if (snapshot) {
        const setter = snapshot.list === "ongoing" ? setOngoingJobs : setPastJobs;
        setter((prev) => {
          if (prev.some((j) => j.group_id === id)) return prev;
          const next = prev.slice();
          next.splice(snapshot.index, 0, snapshot.row);
          if (snapshot.list === "past") {
            _pastCache = {
              jobs: next.slice(),
              offset: (pastOffsetRef.current || 0) + 1,
              hasMore: hasMorePast,
            };
            pastOffsetRef.current = (pastOffsetRef.current || 0) + 1;
          }
          return next;
        });
      }
      throw err;
    }
  }, [hasMorePast]);

  const markRenderGroupCancelled = useCallback((groupId) => {
    const updated = {
      status: "cancelled",
      completed_at: new Date().toISOString(),
      error: i18n.t("shared:job.cancelledByUser"),
    };
    transitionToPast(groupId, updated);
  }, [transitionToPast]);

  // Patch a single group's row from outside the hook.  Used by the
  // detail-page refresh button.  Routes by current status, handling
  // both directions of transition: ongoing<->past.  Without the
  // past->ongoing case, a manual retry on a terminal group would
  // silently no-op the UI even though the server flipped the status.
  const updateGroup = useCallback((groupId, data) => {
    if (!groupId || !data) return;
    const updated = normalizeRenderGroup(data);
    if (!updated) return;
    const inOngoing = ongoingRef.current.some((j) => j.group_id === groupId);
    const inPast = pastRef.current.some((j) => j.group_id === groupId);
    if (isTerminal(updated.status)) {
      if (inOngoing) {
        transitionToPast(groupId, updated);
      } else if (inPast) {
        setPastJobs((prev) => {
          const next = prev.map((j) => (j.group_id === groupId ? { ...j, ...updated } : j));
          _pastCache = { jobs: next.slice(), offset: pastOffsetRef.current, hasMore: hasMorePast };
          return next;
        });
      }
      return;
    }
    // Active branch
    if (inPast) {
      transitionToOngoing(groupId, updated);
    } else if (inOngoing) {
      setOngoingJobs((prev) =>
        prev.map((job) => (job.group_id === groupId ? { ...job, ...updated } : job))
      );
    }
    // else: not in either list yet -- the next list-refetch will pick it up.
  }, [transitionToPast, transitionToOngoing, hasMorePast]);

  // Poll active groups; on a status flip to terminal, transition the
  // row out of ongoing into past + cache.
  useEffect(() => {
    let polling = false;
    const poll = async () => {
      if (polling) return;
      const url = backendUrlRef.current;
      if (!url) return;
      const active = ongoingRef.current.filter((j) => j.group_id);
      if (active.length === 0) return;
      polling = true;
      try {
        await Promise.all(
          active.map(async (job) => {
            try {
              const updated = normalizeRenderGroup(await getRenderGroup(url, job.group_id));
              if (!updated) return;
              if (isTerminal(updated.status)) {
                transitionToPast(job.group_id, updated);
                return;
              }
              setOngoingJobs((cur) =>
                cur.map((existing) =>
                  existing.group_id === job.group_id ? { ...existing, ...updated } : existing
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
  }, [transitionToPast]);

  // Refresh both halves.  Past stays cached across navigations, but the
  // explicit Refresh button is the user's escape hatch -- e.g. when a
  // remote-side delete needs to be picked up, or to bust a stale cache.
  // Cheap (one extra request) and matches user expectations.
  const refresh = useCallback(() => {
    const url = backendUrlRef.current;
    if (!url) return;
    _pastCache = null;
    pastOffsetRef.current = 0;
    fetchOngoingFirstPage(url);
    fetchPastFirstPage(url);
  }, [fetchOngoingFirstPage, fetchPastFirstPage]);

  // Mirror state into the past cache whenever it shifts (covers
  // updateGroup transitions too).
  useEffect(() => { writePastCache(); }, [pastJobs, hasMorePast, writePastCache]);

  return {
    ongoingJobs,
    pastJobs,
    loadingOngoing,
    loadingPast,
    loadingMoreOngoing,
    loadingMorePast,
    hasMoreOngoing,
    hasMorePast,
    loadMoreOngoing,
    loadMorePast,
    addRenderGroup,
    removeJob,
    markRenderGroupCancelled,
    updateGroup,
    refresh,
  };
}
