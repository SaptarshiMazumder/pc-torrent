import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open as dialogOpen } from "@tauri-apps/plugin-dialog";
import {
  createDistributedRenderGroup,
  confirmDistributedJob,
  rerenderGroup,
  cancelRenderGroup,
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
  saveGroupAnalysis,
  getGroupAnalysis,
} from "../utils/blendAnalysis";

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

// ─── Reducer ────────────────────────────────────────────────

const INITIAL_STATE = {
  stage: STAGE.IDLE,
  // Source
  file: null, // { name, path, size }
  savedInputId: "",
  savedInputAsset: null,
  // Re-render mode
  reRenderGroupId: "", // set when re-rendering an existing group
  reRenderFilename: "",
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
  renderEngine: "scene_default",
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

    case "LOAD_RERENDER":
      return {
        ...INITIAL_STATE,
        stage: STAGE.CONFIGURING,
        reRenderGroupId: action.groupId,
        reRenderFilename: action.filename,
        groupId: action.groupId, // makes canStart truthy
        analysis: action.analysis,
        ...action.settings,
      };

    case "RESET":
      return { ...INITIAL_STATE };

    default:
      return state;
  }
}

// ─── Component ──────────────────────────────────────────────

export default function CreateRenderPage({ backendUrl, onJobSubmitted, reRenderSource }) {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const {
    stage, file, savedInputId, savedInputAsset,
    reRenderGroupId, reRenderFilename,
    analysis, prepResult,
    frameStart, frameEnd, frameStep,
    sceneName, cameraMode, forceCameraName, viewLayerName, cameraRanges, renderEngine,
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

  // ── Blender detection ───────────────────────────────────
  useEffect(() => {
    invoke("find_blender")
      .then((bin) => { blenderBinRef.current = bin || null; })
      .catch(() => { blenderBinRef.current = null; });
  }, []);

  // ── Re-render mode: pre-populate from existing job ──────
  useEffect(() => {
    if (!reRenderSource) return;
    const local = getGroupAnalysis(reRenderSource.group_id);
    const raw = local || reRenderSource.analysis_snapshot;
    const snap = (raw && typeof raw === "object" && Array.isArray(raw.scenes) && raw.scenes.length > 0) ? raw : null;
    const overrides = reRenderSource.resolved_render_settings || {};
    const settings = snap ? applyAnalysis(snap) : {};

    // Override with the saved render settings from the original job
    if (typeof overrides.scene_name === "string") settings.sceneName = overrides.scene_name;
    if (typeof overrides.camera_mode === "string") settings.cameraMode = overrides.camera_mode || "auto_markers";
    if (typeof overrides.camera_name === "string") settings.forceCameraName = overrides.camera_name;
    if (typeof overrides.view_layer === "string") settings.viewLayerName = overrides.view_layer;
    if (typeof overrides.render?.engine === "string") settings.renderEngine = overrides.render.engine || "scene_default";
    if (overrides.camera_mode === "camera_ranges" && Array.isArray(overrides.camera_ranges)) {
      settings.cameraRanges = parseCameraRangeRows(overrides.camera_ranges);
    }
    // Use original frame range
    settings.frameStart = String(reRenderSource.frame_start ?? "");
    settings.frameEnd = String(reRenderSource.frame_end ?? "");
    settings.frameStep = String(reRenderSource.frame_step || 1);

    dispatch({
      type: "LOAD_RERENDER",
      groupId: reRenderSource.group_id,
      filename: reRenderSource.input_filename || reRenderSource.filename || "input.blend",
      analysis: snap,
      settings,
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reRenderSource]);

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
  const sourceLabel = reRenderFilename || (file ? file.name : resolvedSavedAsset ? (resolvedSavedAsset.display_name || resolvedSavedAsset.input_filename) : "");
  const canStart = stage === STAGE.CONFIGURING && groupId && (cameraMode !== "camera_ranges" || cameraValidation.ok);
  const needsUpload = stage === STAGE.CONFIGURING && !groupId && Boolean(file);

  // ── Helpers ─────────────────────────────────────────────
  function applyAnalysis(parsed) {
    const s = Array.isArray(parsed?.scenes) ? parsed.scenes : [];
    const active = s.find((sc) => sc?.is_active) || s[0] || null;
    return {
      frameStart: String(parsed.frame_start ?? ""),
      frameEnd: String(parsed.frame_end ?? ""),
      frameStep: String(parsed.frame_step || 1),
      sceneName: active?.name || "",
      forceCameraName: active?.active_camera || active?.cameras?.[0] || "",
      viewLayerName: active?.view_layers?.[0] || "",
      cameraRanges: active ? buildDefaultCameraRanges(active) : [],
      cameraMode: "auto_markers",
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
    if (typeof overrides.render?.engine === "string") settings.renderEngine = overrides.render.engine || "scene_default";
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
    dispatch({ type: "PICK_FILE", file: { name, path: filePath, size } });
  };

  const handleSelectSaved = (asset) => {
    if (isBusy) return;
    cancelActiveOps();
    dispatch({ type: "PICK_SAVED", id: asset.id, asset });
  };

  // ── Analyze ─────────────────────────────────────────────
  const handleAnalyze = async () => {
    if (savedInputId && !file) {
      await handleUseSavedInput();
      return;
    }
    if (!file) {
      dispatch({ type: "ERROR", message: "Select a .blend or .zip file first" });
      return;
    }

    // Check analysis cache first
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
      const result = await invoke("analyze_and_prepare_blend", { filePath: file.path, blenderBin: blenderBinRef.current });
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
      };

      let note = "";
      if (!analysisPayload) {
        note = "Analysis metadata not returned. Set frame range manually.";
      } else if (!prep.prep_done) {
        const detail = prep.prepare_errors.length ? prep.prepare_errors.join(" | ") : "Preparation incomplete";
        note = `Prepare: ${detail}. Upload will use original file.`;
      }

      // Cache the result
      if (file.path) {
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
  const handleUseSavedInput = async () => {
    const assetId = savedInputId;
    if (!assetId) { dispatch({ type: "ERROR", message: "Select a saved file first" }); return; }

    dispatch({ type: "START_ANALYZE" });
    try {
      const created = await createDistributedRenderGroup(backendUrl, null, null, null, assetId);
      const asset = created.prefill || created.source_asset || resolvedSavedAsset;
      const { settings, analysis: snap } = applySavedAssetSettings(asset);
      dispatch({
        type: "USE_SAVED_INPUT",
        groupId: created.group_id || "",
        savedInputId: assetId,
        asset,
        analysis: snap,
        settings,
      });
      void loadSavedInputs();
    } catch (err) {
      dispatch({ type: "ERROR", message: err?.message || "Failed to load saved file", returnTo: STAGE.IDLE });
    }
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
    if (!groupId) { dispatch({ type: "ERROR", message: "Upload must complete first" }); return; }
    if (cameraMode === "camera_ranges" && !cameraValidation.ok) {
      dispatch({ type: "ERROR", message: cameraValidation.error });
      return;
    }

    dispatch({ type: "START_CONFIRM" });
    const controller = new AbortController();
    abortRef.current = controller;

    const overrides = {
      scene_name: sceneName || null,
      camera_mode: cameraMode,
      camera_name: forceCameraName || null,
      view_layer: viewLayerName || null,
      camera_ranges: cameraMode === "camera_ranges"
        ? cameraValidation.rows?.map((r) => ({ camera_name: r.camera_name, frame_start: r.frame_start, frame_end: r.frame_end, frame_step: r.frame_step, enabled: r.enabled !== false }))
        : [],
      timeline: frameRange ? { frame_start: frameRange.frame_start, frame_end: frameRange.frame_end, frame_step: frameRange.frame_step } : {},
      render: { engine: renderEngine !== "scene_default" ? renderEngine : null },
    };

    try {
      let result;
      if (reRenderGroupId) {
        result = await rerenderGroup(backendUrl, reRenderGroupId, {
          frameStart: frameRange?.frame_start,
          frameEnd: frameRange?.frame_end,
          frameStep: frameRange?.frame_step || 1,
          renderOverrides: overrides,
        });
      } else {
        result = await confirmDistributedJob(backendUrl, groupId, null, frameRange, overrides, null, analysis, controller.signal);
        if (result.needs_frame_input) {
          dispatch({ type: "ERROR", message: result.parse_error || "Server requires manual frame range", returnTo: STAGE.CONFIGURING });
          return;
        }
      }

      dispatch({ type: "CONFIRMED" });
      if (analysis && result.group_id) saveGroupAnalysis(result.group_id, analysis);
      onJobSubmitted(result.group_id, reRenderFilename || file?.name || result.input_filename || "input.blend", result.tasks || [], result.total_frames);
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
    [STAGE.CONFIGURING]: reRenderGroupId ? "Re-render — adjust settings" : groupId ? "Ready to render" : "Configure & upload",
    [STAGE.UPLOADING]: `Uploading... ${Math.min(100, uploadProgress).toFixed(0)}%`,
    [STAGE.CONFIRMING]: "Starting render...",
    [STAGE.DONE]: "Submitted",
  }[stage] || "";

  const showSettings = stage === STAGE.CONFIGURING || stage === STAGE.UPLOADING || stage === STAGE.CONFIRMING;
  const showSourcePicker = stage === STAGE.IDLE && !isBusy;
  const fileKind = file ? fileKindFromName(file.name) : "file";

  // ── Render ──────────────────────────────────────────────
  return (
    <div className="page">
      <h2>Create Render</h2>
      <div className="card submit-panel">

        {/* ── Source picker ── */}
        {showSourcePicker && (
          <div className="upload-source-panel">
            <div className="upload-source-topbar">
              <div className="selected-source-inline-subtle">
                {sourceLabel
                  ? <><span className="selected-source-subtle-label">Selected:</span> <span className="selected-source-subtle-name">{sourceLabel}</span></>
                  : <span className="selected-source-subtle-empty">No file selected</span>}
              </div>
              <button className="btn btn-primary upload-source-analyze-btn" type="button" onClick={handleAnalyze} disabled={!hasSource}>
                Analyze
              </button>
            </div>

            <div className="upload-source-actions">
              <button
                type="button"
                className={`upload-plus-btn ${file ? "local-file-selected" : ""}`}
                onClick={handlePickFile}
                onDragOver={(e) => { e.preventDefault(); }}
                onDrop={handleDrop}
              >
                {file && (
                  <button type="button" className="upload-plus-clear-btn" aria-label="Clear" onClick={(e) => { e.stopPropagation(); dispatch({ type: "CLEAR_SOURCE" }); }}>
                    x
                  </button>
                )}
                <span className={`upload-plus-icon ${file ? `file-kind ${fileKind}` : ""}`} aria-hidden="true">
                  {file ? <FileKindIcon kind={fileKind} /> : "+"}
                </span>
                <span className="upload-plus-title">{file ? file.name : "New File"}</span>
                <span className="upload-plus-subtitle">{file ? formatSizeMb(file.size) : "Drop .blend/.zip here or click"}</span>
              </button>
            </div>

            {/* Saved files list */}
            <div className="saved-picker-panel">
              <div className="saved-picker-head">
                <strong>Recent Files</strong>
                <div className="saved-picker-tools">
                  <input type="text" className="saved-picker-search" placeholder="Search" value={savedSearch} onChange={(e) => setSavedSearch(e.target.value)} />
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

        {/* ── Active source + stage indicator ── */}
        {!showSourcePicker && (
          <div className="submit-file-row">
            <div className="submit-file-main">
              <div className="submit-file-name">{sourceLabel || "No file selected"}</div>
              <div className="submit-file-meta">{stageLabel}</div>
            </div>
            <button type="button" className="btn btn-secondary" onClick={handleReset} disabled={isBusy}>
              {reRenderGroupId ? "Cancel" : "Choose Different File"}
            </button>
          </div>
        )}

        {/* ── Progress bar ── */}
        {(stage === STAGE.ANALYZING || stage === STAGE.UPLOADING || stage === STAGE.CONFIRMING) && (
          <div className="runtime-progress-wrap">
            <div className={`runtime-progress-track ${stage !== STAGE.UPLOADING ? "indeterminate" : ""}`}>
              <div className="runtime-progress-fill" style={{ width: stage === STAGE.UPLOADING ? `${uploadProgress}%` : "40%" }} />
            </div>
            <div className="runtime-progress-meta">
              <span>{stage === STAGE.ANALYZING ? "Analyzing" : stage === STAGE.UPLOADING ? "Uploading" : "Starting"}</span>
              <span>{stage === STAGE.UPLOADING ? `${Math.min(100, uploadProgress).toFixed(1)}%` : "Working..."}</span>
            </div>
          </div>
        )}

        {/* ── Action buttons ── */}
        <div className="submit-action-row">
          {needsUpload && (
            <button className="btn btn-primary submit-primary-btn" type="button" onClick={handleUpload} disabled={isBusy}>
              Upload
            </button>
          )}
          {canStart && (
            <button className="btn btn-primary submit-primary-btn" type="button" onClick={handleStartRender}>
              Start Rendering
            </button>
          )}
          {isBusy && (
            <button className="btn btn-danger submit-primary-btn" type="button" onClick={handleStop}>
              Stop
            </button>
          )}
        </div>

        {/* ── Warnings / notes ── */}
        {analysisNote && !error && <p className="muted" style={{ margin: "8px 0 0" }}>{analysisNote}</p>}
        {error && <p className="error-text">{error}</p>}

        {/* ── Render settings panel ── */}
        {showSettings && analysis && (
          <div className="analysis-tabs-wrap">
            <div className="analysis-tab-panel">
              <div className="manual-range-panel">
                <div className="manual-range-title">Render Settings</div>
                <div className="manual-range-grid">
                  <label className="manual-range-field">
                    <span className="manual-range-label">Scene</span>
                    <select value={sceneName} onChange={(e) => handleSceneChange(e.target.value)} className="manual-range-input">
                      {(scenes.length ? scenes : [{ name: "" }]).map((s) => (
                        <option key={s.name || "default"} value={s.name || ""}>{s.name || "Default Scene"}</option>
                      ))}
                    </select>
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">Camera Mode</span>
                    <select value={cameraMode} onChange={(e) => dispatch({ type: "SET_FIELD", field: "cameraMode", value: e.target.value })} className="manual-range-input">
                      {CAMERA_MODES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">Force Camera</span>
                    <select value={forceCameraName} onChange={(e) => dispatch({ type: "SET_FIELD", field: "forceCameraName", value: e.target.value })} className="manual-range-input" disabled={cameraMode !== "force_camera" || cameras.length === 0}>
                      {cameras.length > 0
                        ? cameras.map((c) => <option key={c} value={c}>{c}</option>)
                        : <option value="">No cameras</option>}
                    </select>
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">View Layer</span>
                    <select value={viewLayerName} onChange={(e) => dispatch({ type: "SET_FIELD", field: "viewLayerName", value: e.target.value })} className="manual-range-input">
                      {viewLayers.length > 0
                        ? viewLayers.map((l) => <option key={l} value={l}>{l}</option>)
                        : <option value="">Default</option>}
                    </select>
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">Render Engine</span>
                    <select value={renderEngine} onChange={(e) => dispatch({ type: "SET_FIELD", field: "renderEngine", value: e.target.value })} className="manual-range-input">
                      <option value="scene_default">Scene Default</option>
                      <option value="BLENDER_EEVEE">EEVEE</option>
                      <option value="CYCLES">Cycles</option>
                      <option value="BLENDER_WORKBENCH">Workbench</option>
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
                                <select value={row.camera_name || ""} onChange={(e) => updateRange(row.id, { camera_name: e.target.value })} className="manual-range-input camera-ranges-input">
                                  {cameras.length > 0 ? cameras.map((c) => <option key={c} value={c}>{c}</option>) : <option value="">No cameras</option>}
                                </select>
                              </td>
                              <td><input type="number" min="1" value={row.frame_start ?? ""} onChange={(e) => updateRange(row.id, { frame_start: e.target.value })} className="manual-range-input camera-ranges-input" /></td>
                              <td><input type="number" min="1" value={row.frame_end ?? ""} onChange={(e) => updateRange(row.id, { frame_end: e.target.value })} className="manual-range-input camera-ranges-input" /></td>
                              <td><input type="number" min="1" value={row.frame_step ?? 1} onChange={(e) => updateRange(row.id, { frame_step: e.target.value })} className="manual-range-input camera-ranges-input" /></td>
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

              <div className="manual-range-panel">
                <div className="manual-range-title">Frame Range</div>
                <div className="manual-range-subtitle">
                  {analysis ? "Auto-filled from analysis. Edit before starting." : "Set the frame range for this render."}
                </div>
                <div className="manual-range-grid">
                  <label className="manual-range-field">
                    <span className="manual-range-label">Start</span>
                    <input type="number" min="1" value={frameStart} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameStart", value: e.target.value })} className="manual-range-input" />
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">End</span>
                    <input type="number" min="1" value={frameEnd} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameEnd", value: e.target.value })} className="manual-range-input" />
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">Step</span>
                    <input type="number" min="1" value={frameStep} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameStep", value: e.target.value })} className="manual-range-input" />
                  </label>
                </div>
              </div>

              {/* Analysis report (warnings/errors) */}
              {prepResult && (prepResult.analysis_warnings?.length > 0 || prepResult.prepare_warnings?.length > 0 || prepResult.analysis_errors?.length > 0 || prepResult.prepare_errors?.length > 0) && (
                <div className="prep-results-panel">
                  {[...(prepResult.analysis_errors || []), ...(prepResult.prepare_errors || [])].map((msg, i) => (
                    <div key={`err-${i}`} className="prep-result-item prep-result-error"><span className="prep-result-tag">[ERROR]</span> {msg}</div>
                  ))}
                  {[...(prepResult.analysis_warnings || []), ...(prepResult.prepare_warnings || [])].map((msg, i) => (
                    <div key={`warn-${i}`} className="prep-result-item prep-result-warning"><span className="prep-result-tag">[WARNING]</span> {msg}</div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Settings when no analysis (manual entry only) */}
        {showSettings && !analysis && (
          <div className="analysis-tabs-wrap">
            <div className="analysis-tab-panel">
              <div className="manual-range-panel">
                <div className="manual-range-title">Frame Range</div>
                <div className="manual-range-subtitle">No analysis data available. Enter the frame range manually.</div>
                <div className="manual-range-grid">
                  <label className="manual-range-field">
                    <span className="manual-range-label">Start</span>
                    <input type="number" min="1" value={frameStart} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameStart", value: e.target.value })} className="manual-range-input" />
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">End</span>
                    <input type="number" min="1" value={frameEnd} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameEnd", value: e.target.value })} className="manual-range-input" />
                  </label>
                  <label className="manual-range-field">
                    <span className="manual-range-label">Step</span>
                    <input type="number" min="1" value={frameStep} onChange={(e) => dispatch({ type: "SET_FIELD", field: "frameStep", value: e.target.value })} className="manual-range-input" />
                  </label>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
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
