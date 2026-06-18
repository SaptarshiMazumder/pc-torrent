import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as dialogOpen } from "@tauri-apps/plugin-dialog";
import {
  createDistributedRenderGroup,
  confirmDistributedJob,
  cancelRenderGroup,
  deleteRenderGroup,
  estimateRenderGroup,
  listInputFiles,
  renameInputFile,
  deleteInputFile,
  getFirebaseToken,
} from "../services/api";
import {
  parseFrameRange,
  countFrames,
  nextRangeId,
  buildDefaultCameraRanges,
  parseCameraRangeRows,
  countRowFramesInRange,
  validateCameraRanges,
  formatSizeMb,
  fileKindFromName,
  formatTimestamp,
  resolveTimestamp,
  getCachedAnalysis,
  setCachedAnalysis,
} from "../utils/blendAnalysis";
import { useError } from "../contexts/ErrorContext";
import { formatCredits } from "../utils/creditsFormat";

// ─── Flow stages ────────────────────────────────────────────
// IDLE          → user picks a file / saved input
// ANALYZING     → headless Blender running
// CONFIGURING   → analysis done (or skipped), user edits settings
// UPLOADING     → file streaming to backend
// CONFIRMING    → POST to start the render
// DONE          → render submitted, about to navigate away
const STAGE = {
  IDLE: "idle",
  ANALYZING: "analyzing",
  CONFIGURING: "configuring",
  UPLOADING: "uploading",
  CONFIRMING: "confirming",
  DONE: "done",
};

const CAMERA_MODES = [
  { value: "auto_markers", label: "Auto (Scene Camera + Marker Cuts)" },
  { value: "force_camera", label: "Force Single Camera" },
  { value: "camera_ranges", label: "Camera Ranges (Editable)" },
];

const SAVED_GROUPS = [
  { key: "today", label: "Today" },
  { key: "previous7", label: "Previous 7 days" },
  { key: "previous30", label: "Previous 30 days" },
  { key: "earlier", label: "Earlier" },
];

const HEAVY_FEATURE_LABELS = {
  uses_subdivision: "Subdivision",
  uses_displacement: "Displacement",
  uses_particles: "Particles",
  uses_geometry_nodes: "Geometry Nodes",
  uses_subsurface_scattering: "SSS",
  uses_volumetrics: "Volumetrics",
};

function formatStatNumber(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatStatBytes(bytes) {
  if (typeof bytes !== "number" || bytes <= 0) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function heavyFeatureChips(heaviness) {
  return Object.entries(HEAVY_FEATURE_LABELS)
    .filter(([flag]) => Boolean(heaviness?.[flag]))
    .map(([, label]) => label);
}

// ─── Reducer ────────────────────────────────────────────────

const INITIAL_STATE = {
  stage: STAGE.IDLE,
  // Source
  file: null, // { name, path, size }
  savedInputId: "",
  savedInputAsset: null,
  // Analysis
  analysis: null, // parsed JSON from headless Blender
  prepResult: null, // { prepared_path, filename, warnings, errors, ... }
  // Render settings (editable by user)
  frameStart: "",
  frameEnd: "",
  frameStep: "1",
  sceneName: "",
  cameraMode: "auto_markers",
  forceCameraName: "",
  viewLayerName: "",
  cameraRanges: [],
  // Editable render settings -- pre-filled by applyAnalysis() from the
  // analyzer snapshot, then mutated by user edits.  No null/sentinel
  // "scene default" branch: an unedited field IS the scene default
  // because we pre-fill it.  Only fields the analyzer actually surfaces
  // are exposed; we don't invent overrides for values the .blend
  // never gave us.
  renderEngine: "",
  resolutionX: null,
  resolutionY: null,
  resolutionPercentage: null,
  cyclesSamples: null,
  // Upload / submission
  groupId: "",
  uploadProgress: 0,
  // Errors
  error: "",
  analysisNote: "", // non-blocking warning from analysis
};

function reducer(state, action) {
  switch (action.type) {
    case "PICK_FILE":
      return { ...INITIAL_STATE, stage: STAGE.IDLE, file: action.file };

    case "PICK_SAVED":
      return { ...INITIAL_STATE, stage: STAGE.IDLE, savedInputId: action.id, savedInputAsset: action.asset };

    case "CLEAR_SOURCE":
      return { ...INITIAL_STATE };

    case "START_ANALYZE":
      return { ...state, stage: STAGE.ANALYZING, error: "", analysisNote: "" };

    case "ANALYZE_DONE": {
      const s = { ...state, stage: STAGE.CONFIGURING, analysis: action.analysis, prepResult: action.prepResult, analysisNote: action.note || "" };
      if (action.settings) Object.assign(s, action.settings);
      return s;
    }

    case "ANALYZE_SKIP":
      return { ...state, stage: STAGE.CONFIGURING, analysisNote: action.note || "", error: "" };

    case "USE_SAVED_INPUT":
      return {
        ...state,
        stage: STAGE.CONFIGURING,
        groupId: action.groupId,
        savedInputId: action.savedInputId,
        savedInputAsset: action.asset,
        analysis: action.analysis || null,
        error: "",
        ...action.settings,
      };

    case "SET_FIELD":
      return { ...state, [action.field]: action.value };

    case "SET_FIELDS":
      return { ...state, ...action.fields };

    case "START_UPLOAD":
      return { ...state, stage: STAGE.UPLOADING, uploadProgress: 0, error: "" };

    case "UPLOAD_PROGRESS":
      return { ...state, uploadProgress: Math.max(state.uploadProgress, action.pct) };

    case "UPLOAD_DONE":
      return { ...state, stage: STAGE.CONFIGURING, groupId: action.groupId, uploadProgress: 100 };

    case "START_CONFIRM":
      return { ...state, stage: STAGE.CONFIRMING, error: "" };

    case "CONFIRMED":
      return { ...state, stage: STAGE.DONE };

    case "ERROR":
      return { ...state, stage: action.returnTo || state.stage, error: action.message };

    case "RESET":
      return { ...INITIAL_STATE };

    default:
      return state;
  }
}

// ─── Component ──────────────────────────────────────────────

export default function CreateRenderPage({ backendUrl, onJobSubmitted }) {
  const { showError } = useError();
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const {
    stage, file, savedInputId, savedInputAsset,
    analysis, prepResult,
    frameStart, frameEnd, frameStep,
    sceneName, cameraMode, forceCameraName, viewLayerName, cameraRanges, renderEngine,
    resolutionX, resolutionY, resolutionPercentage,
    cyclesSamples,
    groupId, uploadProgress,
    error, analysisNote,
  } = state;

  // Saved inputs list (independent of flow)
  const [savedInputs, savedInputsDispatch] = useReducer(
    (s, a) => {
      switch (a.type) {
        case "LOADING": return { ...s, loading: true, error: "" };
        case "LOADED": return { ...s, loading: false, items: a.items };
        case "ERROR": return { ...s, loading: false, error: a.message };
        default: return s;
      }
    },
    { items: [], loading: false, error: "" },
  );
  const [savedSearch, setSavedSearch] = useReducer((_, v) => v, "");

  const blenderBinRef = useRef(null);
  const runIdRef = useRef(0);
  const uploadIdRef = useRef("");
  const abortRef = useRef(null);

  // Cost / wall-time estimate, populated after analysis completes.
  // Shape: { wall_time_seconds, cost_credits, machines } | null
  const [costEstimate, setCostEstimate] = useState(null);
  const [costEstimateLoading, setCostEstimateLoading] = useState(false);

  // User-selected queue priority.  Sent verbatim on confirm-upload;
  // server-side Pydantic clamps + validates.  0 = LOW, 1 = NORMAL,
  // 2 = HIGH.  Default NORMAL so the queue ordering is unchanged for
  // anyone who never touches the selector.
  const [priority, setPriority] = useState(1);

  // ── Blender detection ───────────────────────────────────
  useEffect(() => {
    invoke("find_blender")
      .then((bin) => { blenderBinRef.current = bin || null; })
      .catch(() => { blenderBinRef.current = null; });
  }, []);

  // ── Load saved inputs ───────────────────────────────────
  const loadSavedInputs = useCallback(async () => {
    savedInputsDispatch({ type: "LOADING" });
    try {
      const data = await listInputFiles(backendUrl);
      savedInputsDispatch({ type: "LOADED", items: Array.isArray(data?.files) ? data.files : [] });
    } catch (err) {
      savedInputsDispatch({ type: "ERROR", message: err?.message || "Failed to load saved files" });
    }
  }, [backendUrl]);

  useEffect(() => { loadSavedInputs(); }, [loadSavedInputs]);

  // ── Derived values ──────────────────────────────────────
  const scenes = useMemo(() => (Array.isArray(analysis?.scenes) ? analysis.scenes : []), [analysis]);
  const selectedScene = useMemo(() => {
    if (!scenes.length) return null;
    return scenes.find((s) => s.name === sceneName) || scenes.find((s) => s.is_active) || scenes[0];
  }, [scenes, sceneName]);
  const cameras = useMemo(() => (Array.isArray(selectedScene?.cameras) ? selectedScene.cameras : []), [selectedScene]);
  const viewLayers = useMemo(() => (Array.isArray(selectedScene?.view_layers) ? selectedScene.view_layers : []), [selectedScene]);

  const frameRange = useMemo(() => parseFrameRange(frameStart, frameEnd, frameStep), [frameStart, frameEnd, frameStep]);
  const cameraValidation = useMemo(
    () => cameraMode === "camera_ranges" ? validateCameraRanges(cameraRanges, frameRange) : { ok: true, error: "" },
    [cameraMode, cameraRanges, frameRange],
  );

  // After the pre-fill refactor, ``renderEngine`` always holds either
  // the analyzer-detected engine (default) or the user's explicit
  // override -- never the "scene_default" sentinel.  The cost-preview
  // and submit paths therefore consume it directly.
  const resolvedEngine = renderEngine || null;

  // Single source of truth for the render-overrides body.  Both
  // /pre-render/estimate and confirm-upload consume the same dict — the
  // backend's SceneResolver merges it with the analyzer snapshot at
  // submit time.
  const renderOverridesForServer = useMemo(() => ({
    scene_name: sceneName || null,
    camera_mode: cameraMode,
    camera_name: forceCameraName || null,
    view_layer: viewLayerName || null,
    camera_ranges: cameraMode === "camera_ranges"
      ? cameraValidation.rows?.map((r) => ({
          camera_name: r.camera_name,
          frame_start: r.frame_start,
          frame_end: r.frame_end,
          frame_step: r.frame_step,
          enabled: r.enabled !== false,
        })) || []
      : [],
    timeline: frameRange
      ? { frame_start: frameRange.frame_start, frame_end: frameRange.frame_end, frame_step: frameRange.frame_step }
      : {},
    render: {
      engine: resolvedEngine,
      resolution_x: resolutionX,
      resolution_y: resolutionY,
      resolution_percentage: resolutionPercentage,
      cycles_samples: cyclesSamples,
    },
  }), [sceneName, cameraMode, forceCameraName, viewLayerName, cameraValidation, frameRange, resolvedEngine, resolutionX, resolutionY, resolutionPercentage, cyclesSamples]);
  const rangeCounts = useMemo(() => {
    const m = new Map();
    if (!frameRange) return m;
    for (const r of cameraRanges) m.set(r.id, countRowFramesInRange(r, frameRange));
    return m;
  }, [cameraRanges, frameRange]);

  const resolvedSavedAsset = useMemo(
    () => savedInputs.items.find((i) => i.id === savedInputId) || savedInputAsset,
    [savedInputs.items, savedInputId, savedInputAsset],
  );

  const filteredSaved = useMemo(() => {
    const q = savedSearch.trim().toLowerCase();
    if (!q) return savedInputs.items;
    return savedInputs.items.filter((a) =>
      String(a?.display_name || "").toLowerCase().includes(q) ||
      String(a?.input_filename || "").toLowerCase().includes(q),
    );
  }, [savedInputs.items, savedSearch]);

  const savedSections = useMemo(() => {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const d7 = new Date(todayStart); d7.setDate(d7.getDate() - 7);
    const d30 = new Date(todayStart); d30.setDate(d30.getDate() - 30);
    const g = { today: [], previous7: [], previous30: [], earlier: [] };
    filteredSaved.forEach((a) => {
      const ts = resolveTimestamp(a);
      const e = { asset: a, timestamp: ts };
      if (ts && ts >= todayStart) g.today.push(e);
      else if (ts && ts >= d7) g.previous7.push(e);
      else if (ts && ts >= d30) g.previous30.push(e);
      else g.earlier.push(e);
    });
    for (const k of Object.keys(g)) g[k].sort((a, b) => (b.timestamp?.getTime() || 0) - (a.timestamp?.getTime() || 0));
    return SAVED_GROUPS.map(({ key, label }) => ({ key, label, items: g[key] })).filter((s) => s.items.length > 0);
  }, [filteredSaved]);

  const isBusy = stage === STAGE.ANALYZING || stage === STAGE.UPLOADING || stage === STAGE.CONFIRMING;
  const hasSource = Boolean(file) || Boolean(savedInputId);
  const sourceLabel = file ? file.name : resolvedSavedAsset ? (resolvedSavedAsset.display_name || resolvedSavedAsset.input_filename) : "";
  // Saved-input flow: groupId is empty until Start Render mints it.
  // For new uploads, groupId is set at upload time and a Start click
  // just confirms.  Either path can Start once we're past CONFIGURING
  // with a usable source.
  const canStart = stage === STAGE.CONFIGURING
    && (groupId || savedInputId)
    && (cameraMode !== "camera_ranges" || cameraValidation.ok);
  const needsUpload = stage === STAGE.CONFIGURING && !groupId && Boolean(file);

  // Stateless cost preview.  Fires whenever we have an analyzer
  // snapshot + a parseable frame range -- no group_id needed; the
  // backend computes off (snapshot, overrides) sent in the body and
  // returns without persisting anything.  Refreshes when overrides
  // change so the user can tweak resolution / samples / camera mode
  // and watch the estimate update.
  //
  // Extracted into a ref-tracked callback so the manual "Recalculate"
  // button can re-fire the same fetch without round-tripping through
  // useEffect's dep tracking (otherwise hitting the button when no
  // tracked dep changed would be a no-op).  The ref-based cancellation
  // ensures the latest call always wins if the user spams the button.
  const costEstimateCallSeq = useRef(0);
  const [queueDepth, setQueueDepth] = useState(null);
  const refreshCostEstimate = useCallback(() => {
    if (stage !== STAGE.CONFIGURING || !analysis || !frameRange) {
      setCostEstimate(null);
      setQueueDepth(null);
      return;
    }
    const seq = ++costEstimateCallSeq.current;
    setCostEstimateLoading(true);
    estimateRenderGroup(backendUrl, {
      analysisSnapshot: analysis,
      renderOverrides: renderOverridesForServer,
      frameStart: frameRange.frame_start,
      frameEnd: frameRange.frame_end,
      frameStep: frameRange.frame_step,
      fileSizeBytes: file?.size || savedInputAsset?.r2_input_size_bytes || null,
      priority,
    })
      .then((data) => {
        if (seq !== costEstimateCallSeq.current) return;  // superseded
        setCostEstimate(data?.estimate || null);
        setQueueDepth(data?.queue_depth || null);
        setCostEstimateLoading(false);
      })
      .catch((err) => {
        if (seq !== costEstimateCallSeq.current) return;
        setCostEstimate(null);
        setQueueDepth(null);
        setCostEstimateLoading(false);
        showError({
          title: "Couldn't compute cost estimate",
          message: err?.message || "Backend cost-estimate call failed.",
          detail: err?.body || null,
        });
      });
  }, [stage, analysis, frameRange, renderOverridesForServer, backendUrl, file, savedInputAsset, showError, priority]);

  useEffect(() => {
    refreshCostEstimate();
  }, [refreshCostEstimate]);

  // ── Helpers ─────────────────────────────────────────────
  function applyAnalysis(parsed) {
    const s = Array.isArray(parsed?.scenes) ? parsed.scenes : [];
    const active = s.find((sc) => sc?.is_active) || s[0] || null;
    const h = parsed?.heaviness || {};
    return {
      frameStart: String(parsed.frame_start ?? ""),
      frameEnd: String(parsed.frame_end ?? ""),
      frameStep: String(parsed.frame_step || 1),
      sceneName: active?.name || "",
      forceCameraName: active?.active_camera || active?.cameras?.[0] || "",
      viewLayerName: active?.view_layers?.[0] || "",
      cameraRanges: active ? buildDefaultCameraRanges(active) : [],
      cameraMode: "auto_markers",
      // Pre-fill editable render settings from the analyzer.  An
      // unedited field equals the scene default; user edits replace
      // these values in place.  No null/sentinel branch downstream.
      renderEngine: active?.engine || h.render_engine || "",
      resolutionX: Number.isFinite(h.resolution_x) && h.resolution_x > 0 ? h.resolution_x : null,
      resolutionY: Number.isFinite(h.resolution_y) && h.resolution_y > 0 ? h.resolution_y : null,
      resolutionPercentage: Number.isFinite(h.resolution_percentage) && h.resolution_percentage > 0 ? h.resolution_percentage : null,
      cyclesSamples: Number.isFinite(h.samples) && h.samples > 0 ? h.samples : null,
    };
  }

  function applySavedAssetSettings(asset) {
    const snapshot = asset?.analysis_snapshot && typeof asset.analysis_snapshot === "object" ? asset.analysis_snapshot : null;
    const settings = snapshot ? applyAnalysis(snapshot) : {};
    const overrides = asset?.render_overrides && typeof asset.render_overrides === "object" ? asset.render_overrides : {};
    const tl = overrides?.timeline && typeof overrides.timeline === "object" ? overrides.timeline : {};
    const pf = asset?.prefill_frame_range && typeof asset.prefill_frame_range === "object" ? asset.prefill_frame_range : null;

    if (Number.isInteger(pf?.frame_start)) settings.frameStart = String(pf.frame_start);
    else if (Number.isInteger(tl?.frame_start)) settings.frameStart = String(tl.frame_start);
    if (Number.isInteger(pf?.frame_end)) settings.frameEnd = String(pf.frame_end);
    else if (Number.isInteger(tl?.frame_end)) settings.frameEnd = String(tl.frame_end);
    if (Number.isInteger(pf?.frame_step) && pf.frame_step > 0) settings.frameStep = String(pf.frame_step);
    else if (Number.isInteger(tl?.frame_step) && tl.frame_step > 0) settings.frameStep = String(tl.frame_step);

    if (typeof overrides.scene_name === "string") settings.sceneName = overrides.scene_name;
    if (typeof overrides.camera_mode === "string") settings.cameraMode = overrides.camera_mode || "auto_markers";
    if (typeof overrides.camera_name === "string") settings.forceCameraName = overrides.camera_name;
    if (typeof overrides.view_layer === "string") settings.viewLayerName = overrides.view_layer;
    const r = overrides.render && typeof overrides.render === "object" ? overrides.render : {};
    if (typeof r.engine === "string" && r.engine) settings.renderEngine = r.engine;
    if (Number.isInteger(r.resolution_x) && r.resolution_x > 0) settings.resolutionX = r.resolution_x;
    if (Number.isInteger(r.resolution_y) && r.resolution_y > 0) settings.resolutionY = r.resolution_y;
    if (Number.isInteger(r.resolution_percentage) && r.resolution_percentage > 0) settings.resolutionPercentage = r.resolution_percentage;
    if (Number.isInteger(r.cycles_samples) && r.cycles_samples > 0) settings.cyclesSamples = r.cycles_samples;
    if (overrides.camera_mode === "camera_ranges" && Array.isArray(overrides.camera_ranges)) {
      settings.cameraRanges = parseCameraRangeRows(overrides.camera_ranges);
    }
    return { settings, analysis: snapshot };
  }

  function cancelActiveOps() {
    runIdRef.current += 1;
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    const uid = uploadIdRef.current;
    if (uid) {
      invoke("cancel_upload_progress", { uploadId: uid }).catch(() => {});
      invoke("clear_upload_progress", { uploadId: uid }).catch(() => {});
      uploadIdRef.current = "";
    }
  }

  // Phantom cleanup: when the user abandons an in-flight upload by
  // picking a different source, resetting, or leaving the page, the
  // server-side render_group it created has no jobs attached and no
  // path to ever reach a terminal state.  Fire DELETE so it doesn't
  // accumulate in the user's My Jobs list.  Server-side delete now
  // cancels any in-flight orchestrator state before removing the row.
  function discardPendingGroupId(gid) {
    if (!gid) return;
    deleteRenderGroup(backendUrl, gid).catch(() => {});
  }

  // ── File picker ─────────────────────────────────────────
  const handlePickFile = async () => {
    try {
      const selected = await dialogOpen({
        multiple: false,
        filters: [{ name: "Blender project", extensions: ["blend", "zip"] }],
      });
      if (!selected) return;
      const filePath = typeof selected === "string" ? selected : selected.path;
      const name = filePath.replace(/\\/g, "/").split("/").pop();
      let size = 0;
      try { size = await invoke("get_file_size", { filePath }); } catch { /* ok */ }
      cancelActiveOps();
      if (groupId && stage !== STAGE.DONE) discardPendingGroupId(groupId);
      dispatch({ type: "PICK_FILE", file: { name, path: filePath, size } });
    } catch (err) {
      dispatch({ type: "ERROR", message: `Could not open file picker: ${err}` });
    }
  };

  const handleDrop = async (event) => {
    event.preventDefault();
    if (isBusy) return;
    const dropped = event?.dataTransfer?.files?.[0];
    if (!dropped) return;
    const name = String(dropped.name || "").trim();
    const ext = name.toLowerCase().split(".").pop() || "";
    if (ext !== "blend" && ext !== "zip") {
      dispatch({ type: "ERROR", message: "Only .blend or .zip files are supported" });
      return;
    }
    const filePath = dropped.path || null;
    if (!filePath) {
      dispatch({ type: "ERROR", message: "Dropped file path unavailable. Use the file picker instead." });
      return;
    }
    let size = Number.isFinite(dropped.size) && dropped.size > 0 ? dropped.size : 0;
    if (!size) { try { size = await invoke("get_file_size", { filePath }); } catch { /* ok */ } }
    cancelActiveOps();
    if (groupId && stage !== STAGE.DONE) discardPendingGroupId(groupId);
    dispatch({ type: "PICK_FILE", file: { name, path: filePath, size } });
  };

  const handleSelectSaved = (asset) => {
    if (isBusy) return;
    cancelActiveOps();
    if (groupId && stage !== STAGE.DONE) discardPendingGroupId(groupId);
    dispatch({ type: "PICK_SAVED", id: asset.id, asset });
  };

  // ── Analyze ─────────────────────────────────────────────
  // ``deepSearch`` is opt-in -- by default the prepare script skips its
  // tier-4 drive walk for missing-file recovery (fast).  The Analysis
  // Report's "Search this machine for missing files" button calls
  // through with deepSearch=true; that bypasses the cache, runs Blender
  // with PCR_DEEP_SEARCH=1, and the result deliberately isn't cached
  // (a re-pick should re-run the cheap path, not replay the deep one).
  const handleAnalyze = async (deepSearch = false) => {
    if (savedInputId && !file) {
      await handleUseSavedInput();
      return;
    }
    if (!file) {
      dispatch({ type: "ERROR", message: "Select a .blend or .zip file first" });
      return;
    }

    // Check analysis cache first -- but skip when the user explicitly
    // asked for a deep-search re-analyze.
    if (!deepSearch) {
      const cached = file.path ? getCachedAnalysis(file.path, file.size) : null;
      if (cached) {
        dispatch({
          type: "ANALYZE_DONE",
          analysis: cached.analysis,
          prepResult: cached.prepResult,
          settings: cached.analysis ? applyAnalysis(cached.analysis) : {},
          note: cached.note || "",
        });
        return;
      }
    }

    dispatch({ type: "START_ANALYZE" });
    const runId = ++runIdRef.current;

    if (!blenderBinRef.current) {
      dispatch({ type: "ANALYZE_SKIP", note: "Blender not found — analysis skipped. You can still upload and set frame range manually." });
      return;
    }
    if (!file.path) {
      dispatch({ type: "ANALYZE_SKIP", note: "File path unavailable — re-select with file picker to enable analysis." });
      return;
    }

    try {
      const result = await invoke("analyze_and_prepare_blend", { filePath: file.path, blenderBin: blenderBinRef.current, deepSearch });
      if (runId !== runIdRef.current) return;

      const analysisPayload = result?.analysis && typeof result.analysis === "object" ? result.analysis : null;
      const prep = {
        prepared_path: result?.prepared_path || null,
        filename: result?.filename || file.name,
        analysis_warnings: result?.analysis_warnings || [],
        analysis_errors: result?.analysis_errors || [],
        prepare_warnings: result?.prepare_warnings || result?.warnings || [],
        prepare_errors: result?.prepare_errors || result?.errors || [],
        prep_done: Boolean(result?.prep_done),
        deep_search_ran: deepSearch,
      };

      let note = "";
      if (!analysisPayload) {
        note = "Analysis metadata not returned. Set frame range manually.";
      } else if (!prep.prep_done) {
        const detail = prep.prepare_errors.length ? prep.prepare_errors.join(" | ") : "Preparation incomplete";
        note = `Prepare: ${detail}. Upload will use original file.`;
      }

      // Only cache the cheap default analyze.  Deep-search results are
      // one-shot -- next file pick should still get the fast path.
      if (!deepSearch && file.path) {
        setCachedAnalysis(file.path, file.size, { analysis: analysisPayload, prepResult: prep, note });
      }

      dispatch({
        type: "ANALYZE_DONE",
        analysis: analysisPayload,
        prepResult: prep,
        settings: analysisPayload ? applyAnalysis(analysisPayload) : {},
        note,
      });
    } catch (err) {
      if (runId !== runIdRef.current) return;
      dispatch({ type: "ANALYZE_SKIP", note: `Analysis failed: ${err?.message || err}. You can still upload.` });
    }
  };

  // ── Use saved input ─────────────────────────────────────
  // We deliberately do NOT create a render_group here.  The asset row
  // already carries prefill (analysis_snapshot / render_overrides /
  // prefill_frame_range) via /me/input-files, so we can drive the
  // configure stage off the in-memory asset alone.  The real group is
  // minted at Start Render time -- no phantom rows accumulate when the
  // user picks Analyze and then walks away.
  const handleUseSavedInput = () => {
    const assetId = savedInputId;
    if (!assetId) { dispatch({ type: "ERROR", message: "Select a saved file first" }); return; }
    const asset = resolvedSavedAsset;
    if (!asset) { dispatch({ type: "ERROR", message: "Saved file metadata unavailable" }); return; }

    const { settings, analysis: snap } = applySavedAssetSettings(asset);
    dispatch({
      type: "USE_SAVED_INPUT",
      groupId: "",
      savedInputId: assetId,
      asset,
      analysis: snap,
      settings,
    });
  };

  // ── Upload ──────────────────────────────────────────────
  const handleUpload = async () => {
    if (!file) { dispatch({ type: "ERROR", message: "No file selected" }); return; }
    if (!file.path) { dispatch({ type: "ERROR", message: "File path unavailable. Re-select with the file picker." }); return; }

    dispatch({ type: "START_UPLOAD" });

    let uploadPath = file.path;
    let uploadFilename = file.name;
    if (prepResult?.prep_done && prepResult?.prepared_path) {
      uploadPath = prepResult.prepared_path;
      uploadFilename = prepResult.filename || file.name;
    }

    let createdGroupId = "";
    let uploadTaskId = "";
    try {
      const created = await createDistributedRenderGroup(backendUrl, null, uploadFilename, Number.isFinite(file.size) ? file.size : null);
      createdGroupId = created.group_id || "";

      const authToken = await getFirebaseToken();
      uploadTaskId = await invoke("start_upload_file_to_render_group_multipart", {
        filePath: uploadPath,
        backendUrl,
        groupId: createdGroupId,
        authToken: authToken || null,
      });
      uploadIdRef.current = uploadTaskId;

      while (true) {
        await new Promise((r) => setTimeout(r, 250));
        const snap = await invoke("get_upload_progress", { uploadId: uploadTaskId });
        const bytes = Number(snap?.uploaded_bytes);
        const total = Number(snap?.total_bytes);
        const pct = Number.isFinite(bytes) && Number.isFinite(total) && total > 0
          ? Math.min(100, Math.max(0, (bytes / total) * 100))
          : 0;
        dispatch({ type: "UPLOAD_PROGRESS", pct });
        if (snap?.status === "completed") { dispatch({ type: "UPLOAD_PROGRESS", pct: 100 }); break; }
        if (snap?.status === "cancelled") throw new DOMException(snap?.error || "Upload cancelled", "AbortError");
        if (snap?.status === "failed") throw new Error(snap?.error || "Upload failed");
      }

      dispatch({ type: "UPLOAD_DONE", groupId: createdGroupId });
    } catch (err) {
      if (err?.name === "AbortError") {
        if (createdGroupId) cancelRenderGroup(backendUrl, createdGroupId).catch(() => {});
        dispatch({ type: "ERROR", message: "Upload cancelled", returnTo: STAGE.CONFIGURING });
      } else {
        dispatch({ type: "ERROR", message: `Upload failed: ${err?.message || err}`, returnTo: STAGE.CONFIGURING });
      }
    } finally {
      if (uploadTaskId) {
        invoke("cancel_upload_progress", { uploadId: uploadTaskId }).catch(() => {});
        invoke("clear_upload_progress", { uploadId: uploadTaskId }).catch(() => {});
      }
      uploadIdRef.current = "";
    }
  };

  // ── Start render ────────────────────────────────────────
  const handleStartRender = async () => {
    if (!groupId && !savedInputId) { dispatch({ type: "ERROR", message: "Upload must complete first" }); return; }
    if (cameraMode === "camera_ranges" && !cameraValidation.ok) {
      dispatch({ type: "ERROR", message: cameraValidation.error });
      return;
    }

    dispatch({ type: "START_CONFIRM" });
    const controller = new AbortController();
    abortRef.current = controller;

    // Saved-input flow defers group creation to here so we don't leave
    // phantom rows when the user picks Analyze and walks away.  Mint
    // the group now, link to the asset, then fall through to the
    // existing confirm path.
    let activeGroupId = groupId;
    if (!activeGroupId && savedInputId) {
      try {
        const created = await createDistributedRenderGroup(backendUrl, null, null, null, savedInputId);
        activeGroupId = created.group_id || "";
        if (!activeGroupId) throw new Error("Server did not return a group_id");
        dispatch({ type: "SET_FIELD", field: "groupId", value: activeGroupId });
      } catch (err) {
        dispatch({ type: "ERROR", message: err?.message || "Failed to create render group", returnTo: STAGE.CONFIGURING });
        abortRef.current = null;
        return;
      }
    }

    // ``renderEngine`` is pre-filled by applyAnalysis() from the active
    // scene's engine, so it's never the legacy "scene_default" sentinel
    // by the time we get here.  Send what's in the form; null only when
    // analysis was skipped entirely.
    const resolvedEngine = renderEngine || null;

    const overrides = {
      scene_name: sceneName || null,
      camera_mode: cameraMode,
      camera_name: forceCameraName || null,
      view_layer: viewLayerName || null,
      camera_ranges: cameraMode === "camera_ranges"
        ? cameraValidation.rows?.map((r) => ({ camera_name: r.camera_name, frame_start: r.frame_start, frame_end: r.frame_end, frame_step: r.frame_step, enabled: r.enabled !== false }))
        : [],
      timeline: frameRange ? { frame_start: frameRange.frame_start, frame_end: frameRange.frame_end, frame_step: frameRange.frame_step } : {},
      render: {
        engine: resolvedEngine,
        resolution_x: resolutionX,
        resolution_y: resolutionY,
        resolution_percentage: resolutionPercentage,
        cycles_samples: cyclesSamples,
      },
    };

    try {
      const result = await confirmDistributedJob(
        backendUrl, activeGroupId, null, frameRange, overrides, null, analysis, null, priority, controller.signal,
      );
      if (result.needs_frame_input) {
        dispatch({ type: "ERROR", message: result.parse_error || "Server requires manual frame range", returnTo: STAGE.CONFIGURING });
        return;
      }

      dispatch({ type: "CONFIRMED" });
      onJobSubmitted(result.group_id, file?.name || result.input_filename || "input.blend", result.tasks || [], result.total_frames);
      void loadSavedInputs();
    } catch (err) {
      dispatch({ type: "ERROR", message: err?.name === "AbortError" ? "Cancelled" : (err?.message || "Failed to start render"), returnTo: STAGE.CONFIGURING });
    } finally {
      abortRef.current = null;
    }
  };

  // ── Cancel / reset ──────────────────────────────────────
  const handleStop = async () => {
    cancelActiveOps();
    if (groupId && stage !== STAGE.DONE) {
      cancelRenderGroup(backendUrl, groupId).catch(() => {});
    }
    dispatch({ type: "RESET" });
  };

  const handleReset = () => {
    cancelActiveOps();
    if (groupId && stage !== STAGE.DONE) discardPendingGroupId(groupId);
    dispatch({ type: "RESET" });
  };

  // ── Scene selection change ──────────────────────────────
  const handleSceneChange = (name) => {
    const sc = scenes.find((s) => s.name === name);
    const fields = { sceneName: name };
    if (sc) {
      fields.forceCameraName = sc.active_camera || sc.cameras?.[0] || "";
      fields.viewLayerName = sc.view_layers?.[0] || "";
      fields.cameraRanges = buildDefaultCameraRanges(sc);
      if (Number.isInteger(sc.frame_start)) fields.frameStart = String(sc.frame_start);
      if (Number.isInteger(sc.frame_end)) fields.frameEnd = String(sc.frame_end);
      if (Number.isInteger(sc.frame_step) && sc.frame_step > 0) fields.frameStep = String(sc.frame_step);
    }
    dispatch({ type: "SET_FIELDS", fields });
  };

  // ── Saved file management ──────────────────────────────
  const handleRenameSaved = async (asset) => {
    const next = window.prompt("Rename saved file", asset?.display_name || asset?.input_filename || "");
    if (!next?.trim()) return;
    try { await renameInputFile(backendUrl, asset.id, next.trim()); await loadSavedInputs(); }
    catch (err) { dispatch({ type: "ERROR", message: err?.message || "Rename failed" }); }
  };

  const handleDeleteSaved = async (asset) => {
    if (!window.confirm(`Delete "${asset?.display_name || asset?.input_filename}"?`)) return;
    try {
      await deleteInputFile(backendUrl, asset.id);
      if (savedInputId === asset.id) dispatch({ type: "CLEAR_SOURCE" });
      await loadSavedInputs();
    } catch (err) { dispatch({ type: "ERROR", message: err?.message || "Delete failed" }); }
  };

  // ── Camera range editing ────────────────────────────────
  const updateRange = (id, patch) => dispatch({ type: "SET_FIELD", field: "cameraRanges", value: cameraRanges.map((r) => r.id === id ? { ...r, ...patch } : r) });
  const removeRange = (id) => dispatch({ type: "SET_FIELD", field: "cameraRanges", value: cameraRanges.filter((r) => r.id !== id) });
  const addRange = () => {
    const fr = frameRange;
    dispatch({ type: "SET_FIELD", field: "cameraRanges", value: [
      ...cameraRanges,
      { id: nextRangeId(), enabled: true, camera_name: forceCameraName || cameras[0] || "", frame_start: fr?.frame_start ?? 1, frame_end: fr?.frame_end ?? 1, frame_step: fr?.frame_step ?? 1 },
    ]});
  };
  const autoFillRanges = () => {
    if (selectedScene) dispatch({ type: "SET_FIELD", field: "cameraRanges", value: buildDefaultCameraRanges(selectedScene) });
  };

  // ── Derived UI labels ───────────────────────────────────
  const stageLabel = {
    [STAGE.IDLE]: "Select a file",
    [STAGE.ANALYZING]: "Analyzing...",
    [STAGE.CONFIGURING]: groupId ? "Ready to render" : "Configure & upload",
    [STAGE.UPLOADING]: `Uploading... ${Math.min(100, uploadProgress).toFixed(0)}%`,
    [STAGE.CONFIRMING]: "Starting render...",
    [STAGE.DONE]: "Submitted",
  }[stage] || "";

  const showSettings = stage === STAGE.CONFIGURING || stage === STAGE.UPLOADING || stage === STAGE.CONFIRMING;
  const showSourcePicker = stage === STAGE.IDLE && !isBusy;
  const fileKind = file ? fileKindFromName(file.name) : "file";

  // ── Render ──────────────────────────────────────────────
  const totalFrameCount = frameRange ? countFrames(frameRange.frame_start, frameRange.frame_end, frameRange.frame_step) : null;

  // Effective megapixels reflects the form's current resolution values
  // (which were pre-filled from the analyzer at ANALYZE_DONE).  Shown
  // inline next to the resolution inputs as a quick sanity readout.
  const effMpPreview = (resolutionX && resolutionY)
    ? ((resolutionX * resolutionY * ((resolutionPercentage ?? 100) / 100) ** 2) / 1_000_000).toFixed(1)
    : null;

  const numOrNull = (raw) => {
    if (raw === "" || raw == null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
  };

  const formatCost = (entry) => {
    if (!entry) return null;
    const c = entry.cost_credits ?? 0;
    return `${formatCredits(c)} tokens`;
  };
  const formatTime = (entry) => {
    if (!entry?.wall_time_seconds) return null;
    const s = entry.wall_time_seconds;
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
  };

  return (
    <div className="page cr-page">
      <div className="cr-header">
        <h2>Create Render</h2>
        {showSettings && (
          <div className="cr-header-actions">
            {needsUpload && (
              <button className="btn btn-primary cr-start-btn" type="button" onClick={handleUpload} disabled={isBusy}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" /></svg>
                Upload
              </button>
            )}
            {canStart && (
              <button
                className="btn btn-ghost cr-cost-recalc-btn"
                type="button"
                onClick={refreshCostEstimate}
                disabled={costEstimateLoading}
                title="Re-fetch the cost / wall-time estimate from the backend (useful while tuning the analyzer constants)"
                style={{
                  padding: "6px 12px",
                  fontSize: 12,
                  marginRight: 8,
                  opacity: costEstimateLoading ? 0.6 : 1,
                }}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{ marginRight: 5, verticalAlign: "middle" }}>
                  <polyline points="23 4 23 10 17 10" />
                  <polyline points="1 20 1 14 7 14" />
                  <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
                </svg>
                {costEstimateLoading ? "Recalculating…" : "Recalculate"}
              </button>
            )}
            {canStart && (() => {
              const cost = formatCost(costEstimate);
              const time = formatTime(costEstimate);
              return (
                <span className="cr-cost-pill" title="Estimated cost range and wall-clock time for this render">
                  {costEstimateLoading && !costEstimate ? "…" : cost || "—"}
                  {time ? ` • ${time}` : ""}
                </span>
              );
            })()}
            {canStart && (
              <button className="btn btn-primary cr-start-btn" type="button" onClick={handleStartRender}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polygon points="5 3 19 12 5 21 5 3" /></svg>
                Start Rendering
              </button>
            )}
            {isBusy && (
              <button className="btn btn-danger cr-start-btn" type="button" onClick={handleStop}>Stop</button>
            )}
          </div>
        )}
      </div>

      {/* ── Source picker (IDLE stage) ── */}
      {showSourcePicker && (
        <div className="cr-source-section">
          {/* File drop zone */}
          <div className="cr-source-top">
            <button
              type="button"
              className={`cr-drop-zone ${file ? "cr-drop-zone--has-file" : ""}`}
              onClick={handlePickFile}
              onDragOver={(e) => { e.preventDefault(); }}
              onDrop={handleDrop}
            >
              {file && (
                <button type="button" className="cr-drop-clear" aria-label="Clear" onClick={(e) => { e.stopPropagation(); dispatch({ type: "CLEAR_SOURCE" }); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M18 6L6 18M6 6l12 12" /></svg>
                </button>
              )}
              <span className={`cr-drop-icon ${file ? `file-kind ${fileKind}` : ""}`}>
                {file ? <FileKindIcon kind={fileKind} /> : (
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" /></svg>
                )}
              </span>
              <span className="cr-drop-label">{file ? file.name : "Choose or drop file"}</span>
              <span className="cr-drop-hint">{file ? formatSizeMb(file.size) : ".blend or .zip"}</span>
            </button>

            <button className="btn btn-primary cr-analyze-btn" type="button" onClick={() => handleAnalyze()} disabled={!hasSource}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z" /><circle cx="12" cy="12" r="3" /></svg>
              Analyze &amp; Continue
            </button>
          </div>

          {/* Saved files */}
          <div className="saved-picker-panel">
            <div className="saved-picker-head">
              <strong>Recent Files</strong>
              <div className="saved-picker-tools">
                <input type="text" className="saved-picker-search" placeholder="Search files..." value={savedSearch} onChange={(e) => setSavedSearch(e.target.value)} />
                <button className="btn btn-secondary" type="button" onClick={loadSavedInputs} disabled={savedInputs.loading}>
                  {savedInputs.loading ? "..." : "Refresh"}
                </button>
              </div>
            </div>
            <div className="saved-picker-body">
              <div className="saved-picker-columns"><span>Name</span><span>Date</span><span>Type</span><span>Size</span></div>
              {savedInputs.error && <p className="error-text">{savedInputs.error}</p>}
              {savedInputs.loading && <p className="muted">Loading...</p>}
              {!savedInputs.loading && filteredSaved.length === 0 && (
                <p className="saved-picker-empty">{savedInputs.items.length === 0 ? "No saved files yet." : "No matches."}</p>
              )}
              {savedSections.map((section) => (
                <div key={section.key} className="saved-picker-section">
                  <div className="saved-picker-section-label">{section.label}</div>
                  {section.items.map(({ asset, timestamp }) => {
                    const kind = fileKindFromName(asset.input_filename);
                    const display = asset.display_name || asset.input_filename;
                    return (
                      <div
                        key={asset.id}
                        className={`saved-picker-row ${savedInputId === asset.id ? "selected" : ""}`}
                        role="button" tabIndex={0}
                        onClick={() => handleSelectSaved(asset)}
                        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleSelectSaved(asset); } }}
                      >
                        <div className="saved-picker-row-main">
                          <span className={`saved-file-icon ${kind}`}><FileKindIcon kind={kind} /></span>
                          <span className="saved-picker-name-wrap">
                            <span className="saved-picker-display-name">{display}</span>
                            {display !== asset.input_filename && <span className="saved-picker-original-name">{asset.input_filename}</span>}
                          </span>
                        </div>
                        <span className="saved-picker-date">{formatTimestamp(timestamp)}</span>
                        <span className="saved-picker-type">{kind === "blend" ? "Blend" : kind === "zip" ? "Zip" : "File"}</span>
                        <span className="saved-picker-size">{formatSizeMb(asset?.size_bytes)}</span>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Active file bar (when configuring) ── */}
      {!showSourcePicker && (
        <div className="cr-file-bar">
          <div className="cr-file-bar-icon">
            <FileKindIcon kind={file ? fileKindFromName(file.name) : "blend"} />
          </div>
          <div className="cr-file-bar-info">
            <span className="cr-file-bar-name">{sourceLabel || "No file selected"}</span>
            <span className="cr-file-bar-status">{stageLabel}</span>
          </div>
          <button type="button" className="btn btn-secondary" onClick={handleReset} disabled={isBusy}>
            Change File
          </button>
        </div>
      )}

      {/* ── Progress bar ── */}
      {(stage === STAGE.ANALYZING || stage === STAGE.UPLOADING || stage === STAGE.CONFIRMING) && (
        <div className="cr-progress-wrap">
          <div className={`cr-progress-track ${stage !== STAGE.UPLOADING ? "indeterminate" : ""}`}>
            <div className="cr-progress-fill" style={{ width: stage === STAGE.UPLOADING ? `${uploadProgress}%` : "40%" }} />
          </div>
          <div className="cr-progress-meta">
            <span>{stage === STAGE.ANALYZING ? "Analyzing" : stage === STAGE.UPLOADING ? "Uploading" : "Starting"}</span>
            <span>{stage === STAGE.UPLOADING ? `${Math.min(100, uploadProgress).toFixed(1)}%` : "Working..."}</span>
          </div>
        </div>
      )}

      {/* ── Warnings / notes ── */}
      {analysisNote && !error && <p className="cr-note">{analysisNote}</p>}
      {error && <p className="error-text">{error}</p>}

      {/* ── Settings (single Render Setup card) ── */}
      {showSettings && (
        <div className="cr-settings-grid">
          <div className="cr-card cr-card-form">
            <div className="cr-card-header">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
              <span>Render Setup</span>
            </div>

            {/* Output ----------------------------------------------------- */}
            <section className="cr-form-section">
              <div className="cr-form-section-title">Output</div>
              <div className="cr-form-section-body">
                <div className="cr-field-row">
                  <label className="cr-field">
                    <span className="cr-field-label">Render Engine</span>
                    <select value={renderEngine} onChange={(e) => dispatch({ type: "SET_FIELD", field: "renderEngine", value: e.target.value })} className="cr-input">
                      {renderEngine && !["BLENDER_EEVEE", "CYCLES", "BLENDER_WORKBENCH"].includes(renderEngine) && (
                        <option value={renderEngine}>{renderEngine}</option>
                      )}
                      <option value="BLENDER_EEVEE">EEVEE</option>
                      <option value="CYCLES">Cycles</option>
                      <option value="BLENDER_WORKBENCH">Workbench</option>
                    </select>
                  </label>
                  <label className="cr-field">
                    <span className="cr-field-label">Samples</span>
                    <input
                      type="number" min="1"
                      value={cyclesSamples ?? ""}
                      onChange={(e) => dispatch({ type: "SET_FIELD", field: "cyclesSamples", value: numOrNull(e.target.value) })}
                      className="cr-input"
                    />
                  </label>
                </div>
                <div className="cr-field">
                  <span className="cr-field-label">Resolution</span>
                  <div className="cr-resolution-row">
                    <input
                      type="number" min="1"
                      value={resolutionX ?? ""}
                      onChange={(e) => dispatch({ type: "SET_FIELD", field: "resolutionX", value: numOrNull(e.target.value) })}
                      className="cr-input cr-input-num"
                    />
                    <span className="cr-resolution-sep">×</span>
                    <input
                      type="number" min="1"
                      value={resolutionY ?? ""}
                      onChange={(e) => dispatch({ type: "SET_FIELD", field: "resolutionY", value: numOrNull(e.target.value) })}
                      className="cr-input cr-input-num"
                    />
                    <span className="cr-resolution-sep">@</span>
                    <input
                      type="number" min="1" max="1000"
                      value={resolutionPercentage ?? ""}
                      onChange={(e) => dispatch({ type: "SET_FIELD", field: "resolutionPercentage", value: numOrNull(e.target.value) })}
                      className="cr-input cr-input-pct"
                    />
                    <span className="cr-resolution-suffix">%</span>
                    {effMpPreview && <span className="cr-resolution-mp">{effMpPreview} MP effective</span>}
                  </div>
                </div>
              </div>
            </section>

            {/* Scene & camera -------------------------------------------- */}
            <section className="cr-form-section">
              <div className="cr-form-section-title">Scene &amp; Camera</div>
              <div className="cr-form-section-body">
                <div className="cr-field-row">
                  <label className="cr-field">
                    <span className="cr-field-label">Scene</span>
                    <select value={sceneName} onChange={(e) => handleSceneChange(e.target.value)} className="cr-input">
                      {(scenes.length ? scenes : [{ name: "" }]).map((s) => (
                        <option key={s.name || "default"} value={s.name || ""}>{s.name || "Default Scene"}</option>
                      ))}
                    </select>
                  </label>
                  <label className="cr-field">
                    <span className="cr-field-label">View Layer</span>
                    <select value={viewLayerName} onChange={(e) => dispatch({ type: "SET_FIELD", field: "viewLayerName", value: e.target.value })} className="cr-input">
                      {viewLayers.length > 0 ? viewLayers.map((l) => <option key={l} value={l}>{l}</option>) : <option value="">Default</option>}
                    </select>
                  </label>
                </div>
                <div className="cr-field-row">
                  <label className="cr-field">
                    <span className="cr-field-label">Camera Mode</span>
                    <select value={cameraMode} onChange={(e) => dispatch({ type: "SET_FIELD", field: "cameraMode", value: e.target.value })} className="cr-input">
                      {CAMERA_MODES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  </label>
                  <label className="cr-field">
                    <span className="cr-field-label">Camera</span>
                    <select value={forceCameraName} onChange={(e) => dispatch({ type: "SET_FIELD", field: "forceCameraName", value: e.target.value })} className="cr-input" disabled={cameraMode !== "force_camera" || cameras.length === 0}>
                      {cameras.length > 0 ? cameras.map((c) => <option key={c} value={c}>{c}</option>) : <option value="">No cameras</option>}
                    </select>
                  </label>
                </div>
                {cameraMode === "camera_ranges" && (
                  <div className="camera-ranges-panel">
                    <div className="camera-ranges-head">
                      <div className="manual-range-subtitle camera-ranges-subtitle">Edit camera-to-frame mappings.</div>
                      <div className="camera-ranges-actions">
                        <button type="button" className="btn btn-secondary" onClick={autoFillRanges}>Use Marker Cuts</button>
                        <button type="button" className="btn btn-secondary" onClick={addRange}>Add Range</button>
                      </div>
                    </div>
                    <div className="camera-ranges-table-wrap">
                      <table className="camera-ranges-table">
                        <thead><tr><th>Use</th><th>Camera</th><th>Start</th><th>End</th><th>Step</th><th>Frames</th><th /></tr></thead>
                        <tbody>
                          {cameraRanges.map((row) => (
                            <tr key={row.id}>
                              <td><input type="checkbox" checked={row.enabled !== false} onChange={(e) => updateRange(row.id, { enabled: e.target.checked })} /></td>
                              <td>
                                <select value={row.camera_name || ""} onChange={(e) => updateRange(row.id, { camera_name: e.target.value })} className="cr-input camera-ranges-input">
                                  {cameras.length > 0 ? cameras.map((c) => <option key={c} value={c}>{c}</option>) : <option value="">No cameras</option>}
                                </select>
                              </td>
                              <td><input type="number" min="1" value={row.frame_start ?? ""} onChange={(e) => updateRange(row.id, { frame_start: e.target.value })} className="cr-input camera-ranges-input" /></td>
                              <td><input type="number" min="1" value={row.frame_end ?? ""} onChange={(e) => updateRange(row.id, { frame_end: e.target.value })} className="cr-input camera-ranges-input" /></td>
                              <td><input type="number" min="1" value={row.frame_step ?? 1} onChange={(e) => updateRange(row.id, { frame_step: e.target.value })} className="cr-input camera-ranges-input" /></td>
                              <td><span className="camera-ranges-count">{rangeCounts.get(row.id) || 0}</span></td>
                              <td><button type="button" className="btn btn-secondary camera-ranges-remove" onClick={() => removeRange(row.id)}>Remove</button></td>
                            </tr>
                          ))}
                          {cameraRanges.length === 0 && <tr><td colSpan={7} className="camera-ranges-empty">No ranges. Add one to continue.</td></tr>}
                        </tbody>
                      </table>
                    </div>
                    {frameRange && <div className="manual-range-subtitle camera-ranges-subtitle">{countFrames(frameRange.frame_start, frameRange.frame_end, frameRange.frame_step)} frames after step filtering.</div>}
                    {!cameraValidation.ok && <p className="error-text camera-ranges-error">{cameraValidation.error}</p>}
                  </div>
                )}
              </div>
            </section>

            {/* Frames ---------------------------------------------------- */}
            <section className="cr-form-section">
              <div className="cr-form-section-title">
                Frames
                {totalFrameCount != null && <span className="cr-form-section-badge">{totalFrameCount} frames</span>}
              </div>
              <div className="cr-form-section-body">
                <div className="cr-frame-fields">
                  <label className="cr-field">
                    <span className="cr-field-label">Start</span>
                    <input type="number" min="1" value={frameStart} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameStart", value: e.target.value })} className="cr-input" />
                  </label>
                  <label className="cr-field">
                    <span className="cr-field-label">End</span>
                    <input type="number" min="1" value={frameEnd} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameEnd", value: e.target.value })} className="cr-input" />
                  </label>
                  <label className="cr-field">
                    <span className="cr-field-label">Step</span>
                    <input type="number" min="1" value={frameStep} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameStep", value: e.target.value })} className="cr-input" />
                  </label>
                </div>
              </div>
            </section>
            {/* Queue priority ------------------------------------------- */}
            <section className="cr-form-section">
              <div className="cr-form-section-title">Queue Priority</div>
              <div className="cr-form-section-body">
                <div className="cr-priority-group" role="radiogroup" aria-label="Queue priority">
                  {[
                    { value: 0, label: "Low",    hint: "100% cost · runs last" },
                    { value: 1, label: "Normal", hint: "110% cost · default" },
                    { value: 2, label: "High",   hint: "120% cost · runs first" },
                  ].map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      role="radio"
                      aria-checked={priority === opt.value}
                      onClick={() => setPriority(opt.value)}
                      className={`cr-priority-option${priority === opt.value ? " cr-priority-option--active" : ""}`}
                    >
                      <span className="cr-priority-option-label">{opt.label}</span>
                      <span className="cr-priority-option-hint">{opt.hint}</span>
                    </button>
                  ))}
                </div>
              </div>
            </section>
            {/* Scene Stats (read-only -- analyzer metadata) -------------- */}
            {analysis?.heaviness && (
              <section className="cr-form-section">
                <div className="cr-form-section-title">Scene Stats</div>
                <div className="cr-form-section-body">
                  <div className="cr-stat-grid">
                    <div className="cr-stat"><span className="cr-stat-label">Vertices</span><span className="cr-stat-value">{formatStatNumber(analysis.heaviness.vertex_count_total)}</span></div>
                    <div className="cr-stat"><span className="cr-stat-label">Objects</span><span className="cr-stat-value">{formatStatNumber(analysis.heaviness.object_count)}</span></div>
                    <div className="cr-stat"><span className="cr-stat-label">Meshes</span><span className="cr-stat-value">{formatStatNumber(analysis.heaviness.mesh_count)}</span></div>
                    <div className="cr-stat"><span className="cr-stat-label">Materials</span><span className="cr-stat-value">{formatStatNumber(analysis.heaviness.material_count)}</span></div>
                    <div className="cr-stat"><span className="cr-stat-label">Textures</span><span className="cr-stat-value">{formatStatNumber(analysis.heaviness.texture_count)}</span></div>
                    <div className="cr-stat"><span className="cr-stat-label">Texture size</span><span className="cr-stat-value">{formatStatBytes(analysis.heaviness.texture_total_bytes)}</span></div>
                    <div className="cr-stat"><span className="cr-stat-label">Shader nodes</span><span className="cr-stat-value">{formatStatNumber(analysis.heaviness.shader_node_count_total)}</span></div>
                    {typeof analysis.heaviness.file_size_bytes === "number" && analysis.heaviness.file_size_bytes > 0 && (
                      <div className="cr-stat"><span className="cr-stat-label">Blend size</span><span className="cr-stat-value">{formatStatBytes(analysis.heaviness.file_size_bytes)}</span></div>
                    )}
                  </div>
                  {heavyFeatureChips(analysis.heaviness).length > 0 && (
                    <div className="cr-stat-features">
                      <span className="cr-stat-label">Heavy features</span>
                      <div className="cr-stat-chips">
                        {heavyFeatureChips(analysis.heaviness).map((label) => (
                          <span key={label} className="cr-stat-chip">{label}</span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </section>
            )}
          </div>

          {/* ── Right column: queue panel + (optional) analysis report ── */}
          <aside className="cr-aside">
            {queueDepth && (() => {
              const FLEETS = ["vast", "modal", "community"];
              const totals = ["high", "normal", "low"].reduce((acc, level) => {
                let jobs = 0;
                let frames = 0;
                for (const fleet of FLEETS) {
                  const cell = (queueDepth[fleet] || {})[level] || {};
                  jobs += cell.jobs || 0;
                  frames += cell.frames || 0;
                }
                acc[level] = { jobs, frames };
                return acc;
              }, {});
              return (
                <div className="cr-card cr-card-queue">
                  <div className="cr-card-header">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2"><circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" /></svg>
                    <span>Queue right now</span>
                  </div>
                  <ul className="cr-queue-list">
                    {[
                      { key: "high",   label: "HIGH" },
                      { key: "normal", label: "NORMAL" },
                      { key: "low",    label: "LOW" },
                    ].map((row) => {
                      const t = totals[row.key];
                      return (
                        <li key={row.key} className={`cr-queue-row cr-queue-row--${row.key}`}>
                          <span className="cr-queue-row-label">{row.label}</span>
                          <span className="cr-queue-row-jobs">{t.jobs}</span>
                          {t.jobs > 0 && (
                            <span className="cr-queue-row-frames">{t.frames} frames</span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                  <div className="cr-queue-note">
                    Wait depends on what's ahead of your priority.
                  </div>
                </div>
              );
            })()}

            {prepResult && (prepResult.analysis_warnings?.length > 0 || prepResult.prepare_warnings?.length > 0 || prepResult.analysis_errors?.length > 0 || prepResult.prepare_errors?.length > 0) && (() => {
              const allWarnings = [...(prepResult.analysis_warnings || []), ...(prepResult.prepare_warnings || [])];
              const missingFilePattern = /cannot pack|missing images|missing image|still missing|search budget/i;
              const hasMissingFileIssue = allWarnings.some((msg) => missingFilePattern.test(String(msg)));
              return (
                <div className="cr-card cr-card-warnings">
                  <div className="cr-card-header">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4M12 17h.01" /></svg>
                    <span>Analysis Report</span>
                  </div>
                  {[...(prepResult.analysis_errors || []), ...(prepResult.prepare_errors || [])].map((msg, i) => (
                    <div key={`err-${i}`} className="prep-result-item prep-result-error"><span className="prep-result-tag">[ERROR]</span> {msg}</div>
                  ))}
                  {allWarnings.map((msg, i) => (
                    <div key={`warn-${i}`} className="prep-result-item prep-result-warning"><span className="prep-result-tag">[WARNING]</span> {msg}</div>
                  ))}
                  {hasMissingFileIssue && (
                    <div className="prep-result-action">
                      {prepResult.deep_search_ran ? (
                        <span className="prep-result-action-note">
                          Already searched every fixed drive — these files aren't on this machine. Get them from the .blend's source or upload as a .zip with the assets alongside.
                        </span>
                      ) : (
                        <>
                          <button
                            className="btn btn-secondary"
                            type="button"
                            onClick={() => handleAnalyze(true)}
                            disabled={isBusy}
                          >
                            {stage === STAGE.ANALYZING ? "Searching this machine…" : "Search this machine for missing files"}
                          </button>
                          <span className="prep-result-action-note">
                            Walks every fixed drive looking for the missing files by filename. Up to ~90 seconds.
                          </span>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })()}
          </aside>
        </div>
      )}

      {/* Action row when in source picker */}
      {showSourcePicker && (
        <div className="submit-action-row">
          {needsUpload && (
            <button className="btn btn-primary submit-primary-btn" type="button" onClick={handleUpload} disabled={isBusy}>Upload</button>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Small SVG icons (kept inline to avoid extra files) ─────

function FileKindIcon({ kind }) {
  if (kind === "zip") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 3h7l4 4v14H7z" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
        <path d="M14 3v4h4" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
        <rect x="11" y="7.5" width="2" height="2" rx="0.4" fill="currentColor" />
        <rect x="11" y="10.4" width="2" height="2" rx="0.4" fill="currentColor" />
        <rect x="11" y="13.3" width="2" height="2" rx="0.4" fill="currentColor" />
        <rect x="11" y="16.2" width="2" height="2" rx="0.4" fill="currentColor" />
      </svg>
    );
  }
  if (kind === "blend") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3.8 12.2l7.8-5.7v3.7h4.6a5.2 5.2 0 1 1 0 4.1h-3.1" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="16.6" cy="12.2" r="2.1" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 3h7l4 4v14H7z" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
      <path d="M14 3v4h4" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
    </svg>
  );
}
