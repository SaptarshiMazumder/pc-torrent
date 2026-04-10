/**
 * Pure utility functions for blend file analysis, camera ranges,
 * and frame range parsing. No React, no side effects.
 */

// ── Frame range ─────────────────────────────────────────────

export function parseFrameRange(frameStart, frameEnd, frameStep) {
  const fs = Number.parseInt(frameStart, 10);
  const fe = Number.parseInt(frameEnd, 10);
  const fst = Number.parseInt(frameStep, 10);
  if (!Number.isInteger(fs) || !Number.isInteger(fe) || fs < 1 || fe < fs) return null;
  return { frame_start: fs, frame_end: fe, frame_step: Number.isInteger(fst) && fst > 0 ? fst : 1 };
}

export function countFrames(start, end, step) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || !Number.isInteger(step) || step < 1 || end < start) return 0;
  return Math.floor((end - start) / step) + 1;
}

// ── Camera ranges ───────────────────────────────────────────

let _rangeCounter = 0;
export function nextRangeId() {
  return `cr-${++_rangeCounter}`;
}

export function buildDefaultCameraRanges(scene) {
  if (!scene || !Number.isInteger(scene.frame_start) || !Number.isInteger(scene.frame_end)) return [];
  const step = Number.isInteger(scene.frame_step) && scene.frame_step > 0 ? scene.frame_step : 1;
  const start = scene.frame_start;
  const end = scene.frame_end;
  const cameras = Array.isArray(scene.cameras) ? scene.cameras.filter(Boolean) : [];
  const defaultCam = scene.active_camera || cameras[0] || "";

  const cuts = (Array.isArray(scene.camera_cuts) ? scene.camera_cuts : [])
    .filter((c) => Number.isInteger(c?.frame))
    .sort((a, b) => a.frame - b.frame);

  let currentCam = defaultCam;
  for (const cut of cuts) {
    if (cut.frame > start) break;
    if (cut.camera_name) currentCam = cut.camera_name;
  }

  const segments = [];
  let segStart = start;
  for (const cut of cuts) {
    if (cut.frame <= start) continue;
    if (cut.frame > end) break;
    const segEnd = Math.min(end, cut.frame - 1);
    if (segEnd >= segStart) {
      segments.push({ id: nextRangeId(), enabled: true, camera_name: currentCam || defaultCam, frame_start: segStart, frame_end: segEnd, frame_step: step });
    }
    if (cut.camera_name) currentCam = cut.camera_name;
    segStart = Math.max(segStart, cut.frame);
  }
  if (segStart <= end) {
    segments.push({ id: nextRangeId(), enabled: true, camera_name: currentCam || defaultCam, frame_start: segStart, frame_end: end, frame_step: step });
  }
  if (segments.length === 0) {
    segments.push({ id: nextRangeId(), enabled: true, camera_name: defaultCam, frame_start: start, frame_end: end, frame_step: step });
  }

  const merged = [];
  for (const seg of segments) {
    const prev = merged[merged.length - 1];
    if (prev && prev.camera_name === seg.camera_name && prev.frame_step === seg.frame_step && prev.frame_end + 1 >= seg.frame_start) {
      prev.frame_end = Math.max(prev.frame_end, seg.frame_end);
      continue;
    }
    merged.push({ ...seg });
  }
  return merged;
}

export function parseCameraRangeRows(rows) {
  if (!Array.isArray(rows)) return [];
  return rows
    .map((row) => {
      const s = Number.parseInt(row?.frame_start, 10);
      const e = Number.parseInt(row?.frame_end, 10);
      const st = Number.parseInt(row?.frame_step, 10);
      return {
        id: row?.id || nextRangeId(),
        enabled: row?.enabled !== false,
        camera_name: typeof row?.camera_name === "string" ? row.camera_name : "",
        frame_start: s,
        frame_end: e,
        frame_step: Number.isInteger(st) && st > 0 ? st : 1,
      };
    })
    .filter((r) => Number.isInteger(r.frame_start) && Number.isInteger(r.frame_end) && r.frame_end >= r.frame_start);
}

function rowMatchesFrame(row, frame) {
  if (!row || row.enabled === false) return false;
  if (frame < row.frame_start || frame > row.frame_end) return false;
  const step = Number.isInteger(row.frame_step) && row.frame_step > 0 ? row.frame_step : 1;
  return (frame - row.frame_start) % step === 0;
}

export function countRowFramesInRange(row, range) {
  if (!range) return 0;
  let count = 0;
  for (let f = range.frame_start; f <= range.frame_end; f += range.frame_step) {
    if (rowMatchesFrame(row, f)) count += 1;
  }
  return count;
}

export function validateCameraRanges(rows, frameRange) {
  const enabled = parseCameraRangeRows(rows).filter((r) => r.enabled);
  if (enabled.length === 0) return { ok: false, error: "Add at least one enabled camera range." };
  for (const r of enabled) {
    if (!r.camera_name) return { ok: false, error: "Each enabled camera range needs a camera." };
  }
  if (!frameRange) return { ok: false, error: "Enter a valid frame range first." };
  for (let f = frameRange.frame_start; f <= frameRange.frame_end; f += frameRange.frame_step) {
    if (!enabled.find((r) => rowMatchesFrame(r, f))) {
      return { ok: false, error: `Camera ranges do not cover frame ${f}. Adjust ranges or switch to Auto mode.` };
    }
  }
  return { ok: true, error: "", rows: enabled };
}

// ── Display helpers ─────────────────────────────────────────

export function formatSizeMb(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "Size unknown";
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fileKindFromName(name) {
  const lower = String(name || "").toLowerCase();
  if (lower.endsWith(".blend")) return "blend";
  if (lower.endsWith(".zip")) return "zip";
  return "file";
}

export function formatTimestamp(ts) {
  if (!ts) return "Unknown";
  const now = new Date();
  if (ts.getFullYear() === now.getFullYear() && ts.getMonth() === now.getMonth() && ts.getDate() === now.getDate()) {
    return ts.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  return ts.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

export function resolveTimestamp(asset) {
  const raw = asset?.updated_at || asset?.created_at || asset?.last_used_at || null;
  if (!raw) return null;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// ── Analysis cache (in-memory) ──────────────────────────────
// Keyed by (file path + file size) so re-selecting the same file skips analysis.

const _analysisCache = new Map();

export function analysisCacheKey(filePath, fileSize) {
  return `${filePath}|${fileSize}`;
}

export function getCachedAnalysis(filePath, fileSize) {
  return _analysisCache.get(analysisCacheKey(filePath, fileSize)) || null;
}

export function setCachedAnalysis(filePath, fileSize, result) {
  _analysisCache.set(analysisCacheKey(filePath, fileSize), result);
  if (_analysisCache.size > 50) {
    const first = _analysisCache.keys().next().value;
    _analysisCache.delete(first);
  }
}

// ── Group analysis cache (localStorage) ─────────────────────
// Persists analysis per render group so Re-render can show the full settings UI.

const GROUP_ANALYSIS_LS_KEY = "pcrent_group_analysis";
const GROUP_ANALYSIS_MAX = 100;

function _loadGroupAnalysisStore() {
  try {
    return JSON.parse(localStorage.getItem(GROUP_ANALYSIS_LS_KEY) || "{}");
  } catch {
    return {};
  }
}

function _saveGroupAnalysisStore(store) {
  try {
    localStorage.setItem(GROUP_ANALYSIS_LS_KEY, JSON.stringify(store));
  } catch {
    // Ignore quota errors
  }
}

export function saveGroupAnalysis(groupId, analysis) {
  if (!groupId || !analysis || typeof analysis !== "object") return;
  const store = _loadGroupAnalysisStore();
  store[groupId] = analysis;
  // Evict oldest entries if over limit
  const keys = Object.keys(store);
  if (keys.length > GROUP_ANALYSIS_MAX) {
    keys.slice(0, keys.length - GROUP_ANALYSIS_MAX).forEach((k) => delete store[k]);
  }
  _saveGroupAnalysisStore(store);
}

export function getGroupAnalysis(groupId) {
  if (!groupId) return null;
  const store = _loadGroupAnalysisStore();
  return store[groupId] || null;
}
