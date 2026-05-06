// Local-only memory of which failed chunk job_ids the user has manually
// retried.  The server eventually stops listing them as retryable once
// the new dispatch lands, but during the polling gap the row would
// otherwise sit in "Retrying..." and then vanish — confusing UX.
//
// Writes happen ONLY after the server confirms a retry was accepted.
// On reload the panel reads this set and excludes those job_ids from
// the retry list, so the row stays hidden until the new instance shows
// up via normal polling.

const KEY = "pcrent:retried_chunk_jobs:v1";

export function getRetriedJobIds() {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

export function markJobRetried(jobId) {
  if (!jobId) return;
  try {
    const current = getRetriedJobIds();
    current.add(jobId);
    window.localStorage.setItem(KEY, JSON.stringify(Array.from(current)));
  } catch {
    // Quota / unavailable — UX degrades to the previous "vanishes after
    // polling" behavior, no correctness impact.
  }
}
