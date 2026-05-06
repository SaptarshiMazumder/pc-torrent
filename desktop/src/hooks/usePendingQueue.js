import { useCallback, useEffect, useRef, useState } from "react";
import { getRenderGroupPendingQueue } from "../services/api";

// Detail-page poller for the per-group pending allocation queue.
//
// Polls every POLL_INTERVAL_MS while the frontend math says items are
// expected (group not terminal AND known tasks don't cover every frame).
// Empty responses while items are expected count as misses; after
// MAX_MISSES in a row, polling stops — the chunk is likely lost.
//
// Manual refresh resets the miss counter and the stopped flag.

const POLL_INTERVAL_MS = 10_000;
const MAX_MISSES = 3;
const TERMINAL = new Set(["done", "failed", "cancelled"]);
const COVERAGE_STATUSES = new Set(["pending", "running", "done"]);

function coveredFrames(tasks) {
  let n = 0;
  for (const t of tasks || []) {
    if (!COVERAGE_STATUSES.has(t.status)) continue;
    if (t.frame_start == null || t.frame_end == null) continue;
    const step = t.frame_step || 1;
    n += Math.floor((t.frame_end - t.frame_start) / step) + 1;
  }
  return n;
}

export function expectsPending(tasks, totalFrames, groupStatus) {
  if (TERMINAL.has(groupStatus)) return false;
  if (!totalFrames || totalFrames <= 0) return true;
  return coveredFrames(tasks) < totalFrames;
}

export function usePendingQueue(backendUrl, groupId, { tasks, totalFrames, groupStatus }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [stopped, setStopped] = useState(false);
  const missCountRef = useRef(0);
  const stoppedRef = useRef(false);

  const expects = expectsPending(tasks, totalFrames, groupStatus);

  const fetchOnce = useCallback(
    async (signal) => {
      if (!backendUrl || !groupId) return;
      setLoading(true);
      try {
        const data = await getRenderGroupPendingQueue(backendUrl, groupId, { signal });
        const newItems = Array.isArray(data?.items) ? data.items : [];
        setItems(newItems);
        if (newItems.length > 0) {
          missCountRef.current = 0;
          if (stoppedRef.current) {
            stoppedRef.current = false;
            setStopped(false);
          }
        } else if (expects) {
          // Empty result while math says items expected = miss.
          missCountRef.current += 1;
          if (missCountRef.current >= MAX_MISSES && !stoppedRef.current) {
            stoppedRef.current = true;
            setStopped(true);
          }
        }
      } catch (e) {
        if (e?.name === "AbortError") return;
        // Network/server error — don't increment miss counter; user
        // can manually retry.
      } finally {
        setLoading(false);
      }
    },
    [backendUrl, groupId, expects],
  );

  // Manual refresh — resets stopped + miss counter and refetches.
  const refresh = useCallback(() => {
    stoppedRef.current = false;
    missCountRef.current = 0;
    setStopped(false);
    void fetchOnce();
  }, [fetchOnce]);

  // Reset state on group transitioning to terminal.
  useEffect(() => {
    if (TERMINAL.has(groupStatus)) {
      stoppedRef.current = false;
      missCountRef.current = 0;
      setStopped(false);
      setItems([]);
    }
  }, [groupStatus]);

  // Polling lifecycle.
  useEffect(() => {
    if (!backendUrl || !groupId) return;
    if (TERMINAL.has(groupStatus)) return;
    if (!expects) return;
    if (stopped) return;

    const controller = new AbortController();
    void fetchOnce(controller.signal);
    const interval = setInterval(() => {
      if (stoppedRef.current) return;
      void fetchOnce(controller.signal);
    }, POLL_INTERVAL_MS);

    return () => {
      clearInterval(interval);
      controller.abort();
    };
  }, [backendUrl, groupId, groupStatus, expects, stopped, fetchOnce]);

  return { items, loading, stopped, expectsPending: expects, refresh };
}
