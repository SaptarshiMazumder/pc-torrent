import { useEffect, useMemo } from "react";
import { aggregateLifetime, aggregateLive } from "../utils/telemetryDerive";

// Derives the Stats page's numbers from the render groups the app already
// holds.  Two jobs:
//   1. Pull the ENTIRE terminal history so lifetime totals cover everything,
//      not just the pages My Jobs happened to scroll.  It walks page-by-page:
//      each loadMorePast() flips hasMorePast, which re-runs this effect until
//      the history is exhausted.  Past groups are terminal + slim, so this is
//      a handful of cheap list requests.
//   2. Memoize the pure rollups so re-renders (3s poll ticks) don't recompute
//      unless the underlying lists actually change.
export function useUserTelemetry({
  ongoingJobs,
  pastJobs,
  hasMorePast,
  loadingMorePast,
  loadMorePast,
}) {
  useEffect(() => {
    if (hasMorePast && !loadingMorePast) loadMorePast?.();
  }, [hasMorePast, loadingMorePast, loadMorePast]);

  const lifetime = useMemo(
    () => aggregateLifetime([...ongoingJobs, ...pastJobs]),
    [ongoingJobs, pastJobs],
  );
  const live = useMemo(() => aggregateLive(ongoingJobs), [ongoingJobs]);

  return { lifetime, live, loadingHistory: hasMorePast || loadingMorePast };
}
