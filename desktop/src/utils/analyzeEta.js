import i18n from "../i18n/i18n";

// Track per-phase analyze durations in localStorage so the next run can
// compute an honest overall percent (weighted by average phase wall-time)
// and ETA (remaining-phase averages minus elapsed-in-current-phase time).
//
// Storage shape:
//   forge.analyze.phase_durations = { "<phase>": [s1, s2, ..., sN] }
//
// Only the last MAX_HISTORY samples per phase are kept (~10 runs).

const STORAGE_KEY = "forge.analyze.phase_durations";
const MAX_HISTORY = 10;

// Phase ordering matches prepare_and_analyze.py's prepare() then analyze().
// New phases added to the script should be appended here in run order.
export const ANALYZE_PHASES = [
  "loading_blend",
  "scene_report",
  "fix_output",
  "relative_paths",
  "deep_search",
  "relink_libs",
  "enable_addons",
  "pack_images",
  "pack_fonts",
  "pack_sounds",
  "check_external",
  "bake_drivers",
  "validate",
  "save",
  "analyze_scene",
  "analyze_heaviness",
  "finalizing",
];

// Phase labels (PHASE_LABELS) and indeterminate-phase help texts
// (PHASE_HINTS) are user-facing and now live in the ``createRender``
// locale namespace under ``phase.<key>`` / ``phaseHint.<key>``.  They are
// resolved at call time via ``i18n.t`` so the current language wins.

// Phases with no sub-progress AND unknown duration -- we can't compute
// a meaningful percent or ETA, so the UI shows an indeterminate stripe
// + an elapsed-time counter instead of a lying countdown.
const INDETERMINATE_PHASES = new Set(["loading_blend", "finalizing"]);

// Fallback durations (seconds) used when we have no history yet.  These
// reflect what a small-to-medium scene typically takes -- the relative
// weights matter more than the absolute values for the overall percent.
const DEFAULT_DURATIONS = {
  scene_report: 0.05,
  fix_output: 0.05,
  relative_paths: 0.3,
  deep_search: 0.5,
  relink_libs: 0.2,
  enable_addons: 0.2,
  pack_images: 8.0,
  pack_fonts: 0.3,
  pack_sounds: 0.3,
  check_external: 0.2,
  bake_drivers: 1.5,
  validate: 0.05,
  save: 5.0,
  analyze_scene: 0.5,
  analyze_heaviness: 1.0,
  // Blender's startup + .blend load.  Heavy scenes (5+ GB packed
  // textures, complex graphs) can take many minutes here; treated as
  // indeterminate so the user just sees "Loading Blender..." with an
  // elapsed counter instead of a fake countdown.
  loading_blend: 10.0,
  // Blender's headless-quit cleanup on heavy scenes: releasing the data
  // graph + freeing GPU resources can be many seconds, sometimes a minute.
  finalizing: 8.0,
};

function _readStore() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function _writeStore(store) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // localStorage may be full or disabled; the next run will fall back
    // to DEFAULT_DURATIONS without breaking anything.
  }
}

function _avg(values) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const sum = values.reduce((a, b) => a + b, 0);
  return sum / values.length;
}

/** Per-phase average duration (seconds), falling back to defaults. */
export function getPhaseAverages() {
  const store = _readStore();
  const out = {};
  for (const phase of ANALYZE_PHASES) {
    const recorded = _avg(store[phase]);
    out[phase] = recorded ?? DEFAULT_DURATIONS[phase] ?? 1.0;
  }
  return out;
}

/** Save one run's measured durations.  Only known phases are persisted. */
export function recordPhaseDurations(durations) {
  if (!durations || typeof durations !== "object") return;
  const store = _readStore();
  for (const phase of ANALYZE_PHASES) {
    const seconds = Number(durations[phase]);
    if (!Number.isFinite(seconds) || seconds < 0) continue;
    const list = Array.isArray(store[phase]) ? store[phase].slice(-MAX_HISTORY + 1) : [];
    list.push(seconds);
    store[phase] = list;
  }
  _writeStore(store);
}

/**
 * Compute overall percent + ETA given the current phase state.
 *
 * The model preferentially extrapolates the CURRENT phase's true wall
 * time from real-time sub-progress (e.g. "frame 87/598 after 240s" =>
 * phase will take ~27 min) rather than trusting historical averages,
 * because per-scene variance is huge -- a 1000-driver bake on a heavy
 * scene takes minutes per frame, while a default-cube bake is < 1s.
 * Historical averages only seed the fallback total for phases that
 * haven't reported sub-progress yet.
 *
 * @param {object} state
 * @param {string|null} state.phase           Current phase key (null = not started)
 * @param {number} state.elapsedInPhase       Seconds since the current phase started
 * @param {object} state.subProgress          Optional {current, total} within the phase
 * @returns {{percent: number, etaSeconds: number, label: string}}
 */
export function computeOverall(state) {
  const averages = getPhaseAverages();
  const totalDefault = ANALYZE_PHASES.reduce((acc, p) => acc + averages[p], 0);
  const { phase, elapsedInPhase = 0, subProgress = null } = state || {};

  if (!phase) {
    // Pre-spawn / pre-first-event grace window.  Treat as the implicit
    // ``loading_blend`` phase so the UI shows the same indeterminate
    // animation whether the synthetic event has fired or not yet.
    // ``elapsedInPhase`` is reused as the elapsed-since-analyze-start
    // counter (the caller seeds ``phaseStartedAt`` at dispatch time so
    // this works even if no PHASE event has landed yet).
    return {
      percent: 0,
      etaSeconds: null,
      label: i18n.t("createRender:phase.loading_blend"),
      hint: i18n.t("createRender:phaseHint.loading_blend"),
      indeterminate: true,
      elapsedSeconds: elapsedInPhase,
    };
  }

  const idx = ANALYZE_PHASES.indexOf(phase);
  if (idx < 0) {
    return { percent: 0, etaSeconds: totalDefault, label: phase, hint: null, indeterminate: false };
  }

  // Indeterminate phases: no percent / ETA -- just elapsed time + label.
  if (INDETERMINATE_PHASES.has(phase)) {
    return {
      percent: 0,
      etaSeconds: null,
      label: i18n.t("createRender:phase." + phase),
      hint: i18n.t("createRender:phaseHint." + phase),
      indeterminate: true,
      elapsedSeconds: elapsedInPhase,
    };
  }

  // Time accounted for by fully-completed prior phases (historical avg).
  const completedTime = ANALYZE_PHASES.slice(0, idx)
    .reduce((acc, p) => acc + averages[p], 0);

  const phaseAvg = averages[phase];
  const hasSub = subProgress && subProgress.total > 0 && subProgress.current > 0;
  const frac = hasSub ? Math.min(1, subProgress.current / subProgress.total) : null;

  // Project the TRUE duration of the current phase.  Three signals,
  // strongest first:
  //   1. Linear extrapolation from sub-progress + elapsed (needs a real
  //      elapsed time so we're not dividing tiny noise).
  //   2. Elapsed time alone vs historical average (use max -- we never
  //      shrink past what's already been spent).
  //   3. Pure historical fallback.
  let projectedPhaseTime;
  if (hasSub && elapsedInPhase >= 1) {
    projectedPhaseTime = elapsedInPhase / frac;
  } else {
    projectedPhaseTime = Math.max(phaseAvg, elapsedInPhase);
  }

  // Remaining time in this phase + historical averages for everything
  // that hasn't run yet.
  const remainingThisPhase = Math.max(0, projectedPhaseTime - elapsedInPhase);
  const remainingOtherPhases = ANALYZE_PHASES.slice(idx + 1)
    .reduce((acc, p) => acc + averages[p], 0);
  const etaSeconds = remainingThisPhase + remainingOtherPhases;

  // Percent denominator updates as our projection grows -- this keeps
  // the bar honest when a heavy phase turns out longer than expected.
  const projectedTotal = completedTime + projectedPhaseTime + remainingOtherPhases;
  const consumed = completedTime + elapsedInPhase;
  const percent = projectedTotal > 0
    ? Math.min(100, Math.max(0, (consumed / projectedTotal) * 100))
    : 0;

  return {
    percent,
    etaSeconds,
    label: i18n.t("createRender:phase." + phase),
    hint: null,
    indeterminate: false,
  };
}

/** Format elapsed seconds the same shape as formatEta, without the "left" suffix. */
export function formatElapsed(seconds) {
  if (!Number.isFinite(seconds) || seconds < 1) return i18n.t("createRender:eta.zero");
  const s = Math.round(seconds);
  if (s < 60) return i18n.t("createRender:eta.elapsed.seconds", { n: s });
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? i18n.t("createRender:eta.elapsed.minutesSeconds", { m, s: rem }) : i18n.t("createRender:eta.elapsed.minutes", { m });
  const h = Math.floor(m / 60);
  const mrem = m % 60;
  return mrem ? i18n.t("createRender:eta.elapsed.hoursMinutes", { h, m: mrem }) : i18n.t("createRender:eta.elapsed.hours", { h });
}

/** Human-friendly ETA string. */
export function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 1) return i18n.t("createRender:eta.lessThanSecond");
  const s = Math.round(seconds);
  if (s < 60) return i18n.t("createRender:eta.left.seconds", { n: s });
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? i18n.t("createRender:eta.left.minutesSeconds", { m, s: rem }) : i18n.t("createRender:eta.left.minutes", { m });
  const h = Math.floor(m / 60);
  const mrem = m % 60;
  return mrem ? i18n.t("createRender:eta.left.hoursMinutes", { h, m: mrem }) : i18n.t("createRender:eta.left.hours", { h });
}
