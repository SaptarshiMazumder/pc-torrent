// Persistent cache for terminal render-group detail DTOs.  Terminal data
// (done / failed / cancelled) is frozen by definition — no child job will
// ever change again — so we cache the full DTO in localStorage and skip
// the network round-trip on every subsequent visit.
//
// Active jobs are NOT cached here; they go through the polling list flow.

const KEY_PREFIX = "pcrent:detail:v1:";

export function getTerminalDetailFromCache(groupId) {
  if (!groupId) return null;
  try {
    const raw = window.localStorage.getItem(KEY_PREFIX + groupId);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function setTerminalDetailInCache(groupId, data) {
  if (!groupId || !data) return;
  try {
    window.localStorage.setItem(KEY_PREFIX + groupId, JSON.stringify(data));
  } catch {
    // Quota exceeded or storage unavailable — silently skip.  The next
    // visit will refetch over the network, no correctness impact.
  }
}

export function clearTerminalDetailFromCache(groupId) {
  if (!groupId) return;
  try {
    window.localStorage.removeItem(KEY_PREFIX + groupId);
  } catch {
    // ignore
  }
}

// Remove every cached terminal detail (all groups). Called on a user change so
// one account never reads another's cached job DTOs on a shared device.
export function clearAllTerminalDetail() {
  try {
    for (let i = window.localStorage.length - 1; i >= 0; i--) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(KEY_PREFIX)) window.localStorage.removeItem(k);
    }
  } catch {
    // ignore
  }
}
