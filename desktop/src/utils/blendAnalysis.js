/**
 * Utility functions for blend file analysis, camera ranges, and frame
 * range parsing. No React. User-facing strings (validation messages,
 * display fallbacks) resolve through ``i18n.t`` at call time so the
 * current language wins; everything else is a pure function.
 */

import i18n from "../i18n/i18n";

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
  if (enabled.length === 0) return { ok: false, error: i18n.t("createRender:cameraValidation.addOne") };
  for (const r of enabled) {
    if (!r.camera_name) return { ok: false, error: i18n.t("createRender:cameraValidation.needsCamera") };
  }
  if (!frameRange) return { ok: false, error: i18n.t("createRender:cameraValidation.needFrameRange") };
  // Every rendered frame must be claimed by exactly one enabled range:
  // 0 → coverage gap; >1 → overlap (the frame would map to two cameras,
  // making its {camera}_frame#### output filename ambiguous). Step-aware
  // via rowMatchesFrame, so ranges whose bounds touch but render disjoint
  // frames (different step phase) are not flagged.
  for (let f = frameRange.frame_start; f <= frameRange.frame_end; f += frameRange.frame_step) {
    const matches = enabled.filter((r) => rowMatchesFrame(r, f));
    if (matches.length === 0) {
      return { ok: false, error: i18n.t("createRender:cameraValidation.gap", { frame: f }) };
    }
    if (matches.length > 1) {
      const names = [...new Set(matches.map((r) => r.camera_name))].join(", ");
      return { ok: false, error: i18n.t("createRender:cameraValidation.overlap", { frame: f, names }) };
    }
  }
  return { ok: true, error: "", rows: enabled };
}

// ── Render passes (multilayer EXR) ──────────────────────────
// Catalog of passes the UI can expose.  Keys mirror the worker's
// _PASS_ATTR / _PASS_ATTR_CYCLES_SUB maps so detect -> override
// round-trips 1:1.  ``group`` drives the UI sectioning; ``engines``
// drives engine-aware show/hide (the worker silently no-ops anything
// the active engine doesn't have).  Display labels are user-facing and
// live in the ``createRender`` locale: pass checkbox labels under
// ``output.pass.<key>`` and group titles under ``output.passGroup.<group>``;
// CreateRenderPage resolves them at render via ``t``.
export const PASS_GROUP_ORDER = ["Data", "Light", "Volume", "Cryptomatte", "Denoising"];

export const RENDER_PASS_CATALOG = [
  // Data
  { key: "z", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "mist", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "normal", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "position", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "vector", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "uv", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "object_index", group: "Data", engines: ["CYCLES", "EEVEE"] },
  { key: "material_index", group: "Data", engines: ["CYCLES", "EEVEE"] },
  // Light (Cycles split direct/indirect/color)
  { key: "diffuse_direct", group: "Light", engines: ["CYCLES"] },
  { key: "diffuse_indirect", group: "Light", engines: ["CYCLES"] },
  { key: "diffuse_color", group: "Light", engines: ["CYCLES"] },
  { key: "glossy_direct", group: "Light", engines: ["CYCLES"] },
  { key: "glossy_indirect", group: "Light", engines: ["CYCLES"] },
  { key: "glossy_color", group: "Light", engines: ["CYCLES"] },
  { key: "transmission_direct", group: "Light", engines: ["CYCLES"] },
  { key: "transmission_indirect", group: "Light", engines: ["CYCLES"] },
  { key: "transmission_color", group: "Light", engines: ["CYCLES"] },
  { key: "shadow_catcher", group: "Light", engines: ["CYCLES"] },
  // Light (EEVEE combined-style)
  { key: "diffuse_light", group: "Light", engines: ["EEVEE"] },
  { key: "specular_light", group: "Light", engines: ["EEVEE"] },
  { key: "specular_color", group: "Light", engines: ["EEVEE"] },
  { key: "volume_light", group: "Light", engines: ["EEVEE"] },
  { key: "transparent", group: "Light", engines: ["EEVEE"] },
  // Light (engine-agnostic)
  { key: "emission", group: "Light", engines: ["CYCLES", "EEVEE"] },
  { key: "environment", group: "Light", engines: ["CYCLES", "EEVEE"] },
  { key: "ambient_occlusion", group: "Light", engines: ["CYCLES", "EEVEE"] },
  { key: "shadow", group: "Light", engines: ["CYCLES", "EEVEE"] },
  // Volume (Cycles only)
  { key: "volume_direct", group: "Volume", engines: ["CYCLES"] },
  { key: "volume_indirect", group: "Volume", engines: ["CYCLES"] },
  // Cryptomatte
  { key: "cryptomatte_object", group: "Cryptomatte", engines: ["CYCLES", "EEVEE"] },
  { key: "cryptomatte_material", group: "Cryptomatte", engines: ["CYCLES", "EEVEE"] },
  { key: "cryptomatte_asset", group: "Cryptomatte", engines: ["CYCLES", "EEVEE"] },
  // Denoising (Cycles only; single bool enables Albedo + Normal + Depth)
  { key: "denoising_data", group: "Denoising", engines: ["CYCLES"] },
];

// True if ``pass`` supports the given engine string from the UI
// (``CYCLES`` or ``BLENDER_EEVEE``).  Unknown/empty engine shows all.
export function passSupportsEngine(pass, engine) {
  if (!engine) return true;
  const wanted = engine === "BLENDER_EEVEE" ? "EEVEE" : engine === "CYCLES" ? "CYCLES" : null;
  if (wanted === null) return true;
  return Array.isArray(pass.engines) && pass.engines.includes(wanted);
}

// Pull the passes the .blend already enables for a given view layer out
// of the analyzer payload.  Returns a { passKey: bool } map limited to
// passes the active engine exposes (the analyzer only reports those).
export function prefillRenderPasses(parsed, viewLayerName) {
  const vlp = parsed?.view_layer_passes && typeof parsed.view_layer_passes === "object"
    ? parsed.view_layer_passes : {};
  const detected = vlp[viewLayerName] || vlp[Object.keys(vlp)[0]] || {};
  const out = {};
  for (const { key } of RENDER_PASS_CATALOG) {
    if (key in detected) out[key] = Boolean(detected[key]);
  }
  return out;
}

// ── Display helpers ─────────────────────────────────────────

export function formatSizeMb(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return i18n.t("createRender:display.sizeUnknown");
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fileKindFromName(name) {
  const lower = String(name || "").toLowerCase();
  if (lower.endsWith(".blend")) return "blend";
  if (lower.endsWith(".zip")) return "zip";
  return "file";
}

export function formatTimestamp(ts) {
  if (!ts) return i18n.t("createRender:display.unknown");
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

