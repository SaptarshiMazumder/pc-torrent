import { useEffect, useState } from "react";

/**
 * Stateless 1Hz wall-clock signal.  Returns a number that bumps every
 * ``intervalMs`` so consumers re-render and recompute their inline
 * ``(now - started_at) × price / 3600`` formula.
 *
 * Why a signal (not a value):
 * - The closed-form actual-cost formula depends on three things:
 *   ``started_at`` (immutable per task), ``price_per_hour`` (immutable),
 *   and ``now``.  Since the first two come from the polled DTO, only
 *   ``now`` needs to advance between polls.
 * - Each subscriber computes its own value -- we don't want to thread
 *   per-task state through the hook.
 * - A single ``setInterval`` for the page is cheaper than one per card.
 *
 * @param {number} intervalMs -- how often to bump.  Default 1s.
 * @returns {number} a tick counter; ignore the value, just depend on it.
 */
export function useLiveCostTick(intervalMs = 1000) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return tick;
}

/**
 * Closed-form actual cost given a task's three telemetry fields plus
 * the current wall-clock.  Used by both InstancePanel cards and the
 * Costs drawer to keep the formula in ONE place.
 *
 * Mirrors the server-side formula in serializers.py::_actual_cost so
 * the live-ticked value lines up with the canonical value the next
 * poll will deliver.
 *
 * @param {object} task -- a per-task DTO from the render-group response.
 * @param {Date}   now  -- the wall-clock to use as ``end`` for in-flight tasks.
 * @returns {number | null} the cost in USD, or null when uncomputable.
 */
export function liveActualCost(task, now) {
  const startedAt = task?.started_at;
  if (!startedAt) return null;
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return null;

  let endMs;
  if (task.completed_at) {
    const e = new Date(task.completed_at).getTime();
    if (Number.isNaN(e)) return null;
    endMs = e;
  } else if (task.status === "running" || task.status === "uploading") {
    endMs = now.getTime();
  } else {
    return null;
  }

  const seconds = Math.max(0, (endMs - start) / 1000);
  const rate = typeof task.price_per_hour_at_dispatch === "number"
    ? task.price_per_hour_at_dispatch
    : null;
  if (rate == null) return null;

  return (seconds * rate) / 3600;
}
