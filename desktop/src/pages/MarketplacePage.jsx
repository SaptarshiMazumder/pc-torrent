import { useEffect, useMemo, useState } from "react";
import MachineCard from "../components/MachineCard";
import {
  getMachines,
  createDistributedRenderGroup,
  uploadDistributedRenderInput,
  confirmDistributedJob,
} from "../lib/api";
import { parseBlendFile } from "../lib/blend-parser";

const SEGMENT_COLORS = [
  "#6c63ff",
  "#3b82f6",
  "#10b981",
  "#f59e0b",
  "#ef4444",
  "#8b5cf6",
];

const FLOW_STAGE = {
  IDLE: "idle",
  ANALYZED: "analyzed",
  UPLOADED: "uploaded",
  STARTING: "starting",
  SUBMITTED: "submitted",
};

const CAMERA_MODE_OPTIONS = [
  { value: "auto_markers", label: "Auto (Scene Camera + Marker Cuts)" },
  { value: "force_camera", label: "Force Single Camera" },
  { value: "camera_ranges", label: "Camera Ranges (Editable)" },
];

function powerScore(machine) {
  return (machine.gpu_vram_gb || 0) * 4 + (machine.cpu_cores || 0) + (machine.ram_gb || 0) * 0.3;
}

function parseManualFrameRange(frameStart, frameEnd, frameStep) {
  const fs = Number.parseInt(frameStart, 10);
  const fe = Number.parseInt(frameEnd, 10);
  const fst = Number.parseInt(frameStep, 10);

  if (!Number.isInteger(fs) || !Number.isInteger(fe) || fs < 1 || fe < fs) {
    return null;
  }

  const step = Number.isInteger(fst) && fst > 0 ? fst : 1;
  return { frame_start: fs, frame_end: fe, frame_step: step };
}

function countFramesInRange(start, end, step) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || !Number.isInteger(step) || step < 1 || end < start) {
    return 0;
  }
  return Math.floor((end - start) / step) + 1;
}

function nextCameraRangeId() {
  nextCameraRangeId.counter = (nextCameraRangeId.counter || 0) + 1;
  return `camera-range-${nextCameraRangeId.counter}`;
}

function buildDefaultCameraRanges(scene) {
  if (!scene || !Number.isInteger(scene.frame_start) || !Number.isInteger(scene.frame_end)) {
    return [];
  }
  const sceneStep = Number.isInteger(scene.frame_step) && scene.frame_step > 0 ? scene.frame_step : 1;
  const sceneStart = scene.frame_start;
  const sceneEnd = scene.frame_end;
  const knownCameras = Array.isArray(scene.cameras) ? scene.cameras.filter(Boolean) : [];
  const defaultCamera = scene.active_camera || knownCameras[0] || "";

  const cuts = (Array.isArray(scene.camera_cuts) ? scene.camera_cuts : [])
    .filter((cut) => Number.isInteger(cut?.frame))
    .sort((a, b) => a.frame - b.frame);

  let currentCamera = defaultCamera;
  for (const cut of cuts) {
    if (cut.frame > sceneStart) break;
    if (cut.camera_name) currentCamera = cut.camera_name;
  }

  const segments = [];
  let segmentStart = sceneStart;
  for (const cut of cuts) {
    if (cut.frame <= sceneStart) continue;
    if (cut.frame > sceneEnd) break;

    const segmentEnd = Math.min(sceneEnd, cut.frame - 1);
    if (segmentEnd >= segmentStart) {
      segments.push({
        id: nextCameraRangeId(),
        enabled: true,
        camera_name: currentCamera || defaultCamera,
        frame_start: segmentStart,
        frame_end: segmentEnd,
        frame_step: sceneStep,
      });
    }
    if (cut.camera_name) currentCamera = cut.camera_name;
    segmentStart = Math.max(segmentStart, cut.frame);
  }

  if (segmentStart <= sceneEnd) {
    segments.push({
      id: nextCameraRangeId(),
      enabled: true,
      camera_name: currentCamera || defaultCamera,
      frame_start: segmentStart,
      frame_end: sceneEnd,
      frame_step: sceneStep,
    });
  }

  if (segments.length === 0) {
    segments.push({
      id: nextCameraRangeId(),
      enabled: true,
      camera_name: defaultCamera,
      frame_start: sceneStart,
      frame_end: sceneEnd,
      frame_step: sceneStep,
    });
  }

  const merged = [];
  for (const segment of segments) {
    const prev = merged[merged.length - 1];
    if (
      prev &&
      prev.camera_name === segment.camera_name &&
      prev.frame_step === segment.frame_step &&
      prev.frame_end + 1 >= segment.frame_start
    ) {
      prev.frame_end = Math.max(prev.frame_end, segment.frame_end);
      continue;
    }
    merged.push({ ...segment });
  }
  return merged;
}

function parseCameraRangeRows(rows) {
  if (!Array.isArray(rows)) return [];
  return rows
    .map((row) => {
      const start = Number.parseInt(row?.frame_start, 10);
      const end = Number.parseInt(row?.frame_end, 10);
      const step = Number.parseInt(row?.frame_step, 10);
      return {
        id: row?.id || nextCameraRangeId(),
        enabled: row?.enabled !== false,
        camera_name: typeof row?.camera_name === "string" ? row.camera_name : "",
        frame_start: start,
        frame_end: end,
        frame_step: Number.isInteger(step) && step > 0 ? step : 1,
      };
    })
    .filter((row) => Number.isInteger(row.frame_start) && Number.isInteger(row.frame_end) && row.frame_end >= row.frame_start);
}

function rowMatchesFrame(row, frame) {
  if (!row || row.enabled === false) return false;
  if (frame < row.frame_start || frame > row.frame_end) return false;
  const step = Number.isInteger(row.frame_step) && row.frame_step > 0 ? row.frame_step : 1;
  return (frame - row.frame_start) % step === 0;
}

function countRowFramesWithinTimeline(row, frameRange) {
  if (!frameRange) return 0;
  let count = 0;
  for (let frame = frameRange.frame_start; frame <= frameRange.frame_end; frame += frameRange.frame_step) {
    if (rowMatchesFrame(row, frame)) count += 1;
  }
  return count;
}

function validateCameraRanges(rows, frameRange) {
  const parsedRows = parseCameraRangeRows(rows).filter((row) => row.enabled);
  if (parsedRows.length === 0) {
    return { ok: false, error: "Add at least one enabled camera range.", rows: [] };
  }
  for (const row of parsedRows) {
    if (!row.camera_name) {
      return { ok: false, error: "Each enabled camera range needs a camera.", rows: [] };
    }
  }
  if (!frameRange) {
    return { ok: false, error: "Enter a valid frame range first.", rows: [] };
  }

  for (let frame = frameRange.frame_start; frame <= frameRange.frame_end; frame += frameRange.frame_step) {
    const matched = parsedRows.find((row) => rowMatchesFrame(row, frame));
    if (!matched) {
      return {
        ok: false,
        error: `Camera ranges do not cover frame ${frame}. Adjust ranges or switch to Auto mode.`,
        rows: [],
      };
    }
  }

  return { ok: true, error: "", rows: parsedRows };
}

function SelectedMachinesSummary({ machines }) {
  const totalPower = useMemo(
    () => machines.reduce((sum, machine) => sum + powerScore(machine), 0),
    [machines]
  );

  return (
    <div className="selected-machines-list">
      <h3>
        {machines.length} machine{machines.length !== 1 ? "s" : ""} selected
      </h3>
      <div className="power-distribution-preview">
        {machines.map((machine, index) => {
          const share = totalPower > 0 ? (powerScore(machine) / totalPower) * 100 : 0;
          return (
            <div
              key={machine.id}
              className="power-preview-segment"
              style={{ width: `${Math.max(share, 3)}%` }}
              title={`${machine.gpu_model}: ~${Math.round(share)}%`}
            >
              <div className="power-preview-fill" data-color-index={index % SEGMENT_COLORS.length} />
            </div>
          );
        })}
      </div>
      <div className="power-preview-labels">
        {machines.map((machine, index) => {
          const share = totalPower > 0 ? Math.round((powerScore(machine) / totalPower) * 100) : 0;
          return (
            <div className="power-preview-label" data-color-index={index % SEGMENT_COLORS.length} key={machine.id}>
              <span className="power-label-dot" />
              <span>{machine.gpu_model}</span>
              <span>~{share}% of frames</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function MarketplacePage({ backendUrl, onJobSubmitted }) {
  const [view, setView] = useState("browse");
  const [machines, setMachines] = useState([]);
  const [loadingMachines, setLoadingMachines] = useState(true);
  const [machineError, setMachineError] = useState("");
  const [selectedMachineIds, setSelectedMachineIds] = useState([]);

  const [file, setFile] = useState(null);
  const [flowStage, setFlowStage] = useState(FLOW_STAGE.IDLE);
  const [analyzing, setAnalyzing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);

  const [analysisResult, setAnalysisResult] = useState(null);
  const [clientParseError, setClientParseError] = useState("");
  const [serverParseError, setServerParseError] = useState("");
  const [error, setError] = useState("");

  const [pendingGroupId, setPendingGroupId] = useState("");

  const [frameStart, setFrameStart] = useState("");
  const [frameEnd, setFrameEnd] = useState("");
  const [frameStep, setFrameStep] = useState("1");
  const [sceneName, setSceneName] = useState("");
  const [cameraMode, setCameraMode] = useState("auto_markers");
  const [forceCameraName, setForceCameraName] = useState("");
  const [viewLayerName, setViewLayerName] = useState("");
  const [cameraRanges, setCameraRanges] = useState([]);

  const selectedMachines = useMemo(
    () => machines.filter((machine) => selectedMachineIds.includes(machine.id)),
    [machines, selectedMachineIds]
  );
  const selectedMachineIdSet = useMemo(() => new Set(selectedMachineIds), [selectedMachineIds]);
  const analyzedScenes = useMemo(
    () => (Array.isArray(analysisResult?.scenes) ? analysisResult.scenes : []),
    [analysisResult]
  );
  const selectedSceneInfo = useMemo(() => {
    if (!analyzedScenes.length) return null;
    return (
      analyzedScenes.find((scene) => scene.name === sceneName) ||
      analyzedScenes.find((scene) => scene.is_active) ||
      analyzedScenes[0]
    );
  }, [analyzedScenes, sceneName]);
  const availableCameras = useMemo(
    () => (Array.isArray(selectedSceneInfo?.cameras) ? selectedSceneInfo.cameras : []),
    [selectedSceneInfo]
  );
  const availableViewLayers = useMemo(
    () => (Array.isArray(selectedSceneInfo?.view_layers) ? selectedSceneInfo.view_layers : []),
    [selectedSceneInfo]
  );

  const resetSubmissionFlow = ({ clearFile = false } = {}) => {
    setFlowStage(FLOW_STAGE.IDLE);
    setAnalyzing(false);
    setUploading(false);
    setStarting(false);
    setUploadProgress(0);
    setAnalysisResult(null);
    setClientParseError("");
    setServerParseError("");
    setError("");
    setPendingGroupId("");
    setFrameStart("");
    setFrameEnd("");
    setFrameStep("1");
    setSceneName("");
    setCameraMode("auto_markers");
    setForceCameraName("");
    setViewLayerName("");
    setCameraRanges([]);
    if (clearFile) {
      setFile(null);
    }
  };

  const loadMachines = async () => {
    setLoadingMachines(true);
    setMachineError("");
    try {
      const data = await getMachines(backendUrl);
      setMachines(Array.isArray(data) ? data : []);
    } catch (err) {
      setMachineError(err.message || "Cannot reach backend");
    } finally {
      setLoadingMachines(false);
    }
  };

  useEffect(() => {
    loadMachines();
  }, [backendUrl]);

  useEffect(() => {
    if (view === "submit" && selectedMachines.length === 0) {
      setView("browse");
    }
  }, [view, selectedMachines.length]);

  const handleToggleMachine = (machine) => {
    resetSubmissionFlow();
    setSelectedMachineIds((prev) => {
      if (prev.includes(machine.id)) {
        return prev.filter((id) => id !== machine.id);
      }
      return [...prev, machine.id];
    });
  };

  const handleAnalyze = async () => {
    if (!file) {
      setError("Select a .blend file or .zip project bundle first");
      return;
    }
    if (selectedMachines.length === 0) {
      setError("Select at least one machine before analyzing");
      return;
    }

    setError("");
    setServerParseError("");
    setPendingGroupId("");
    setUploadProgress(0);
    setAnalyzing(true);

    try {
      const parsed = await parseBlendFile(file);
      setAnalysisResult(parsed);
      setClientParseError("");
      setFrameStart(String(parsed.frame_start));
      setFrameEnd(String(parsed.frame_end));
      setFrameStep(String(parsed.frame_step || 1));
      const parsedScenes = Array.isArray(parsed?.scenes) ? parsed.scenes : [];
      const activeScene =
        parsedScenes.find((scene) => scene?.is_active) ||
        parsedScenes.find(Boolean) ||
        null;
      if (activeScene) {
        setSceneName(activeScene.name || "");
        setForceCameraName(activeScene.active_camera || (activeScene.cameras?.[0] ?? ""));
        setViewLayerName(activeScene.view_layers?.[0] ?? "");
        setCameraRanges(buildDefaultCameraRanges(activeScene));
      } else {
        setSceneName("");
        setForceCameraName("");
        setViewLayerName("");
        setCameraRanges([]);
      }
      setCameraMode("auto_markers");
    } catch (err) {
      setAnalysisResult(null);
      setClientParseError(`Client parse failed: ${err.message || "Unknown parsing error"}`);
      setFrameStart("");
      setFrameEnd("");
      setFrameStep("1");
      setSceneName("");
      setCameraMode("auto_markers");
      setForceCameraName("");
      setViewLayerName("");
      setCameraRanges([]);
    } finally {
      setFlowStage(FLOW_STAGE.ANALYZED);
      setAnalyzing(false);
    }
  };

  const handleUpload = async () => {
    if (flowStage !== FLOW_STAGE.ANALYZED) {
      return;
    }
    if (!file) {
      setError("Select a .blend file or .zip project bundle first");
      return;
    }
    if (selectedMachines.length === 0) {
      setError("Select at least one machine before uploading");
      return;
    }

    setError("");
    setServerParseError("");
    setUploadProgress(0);
    setUploading(true);
    try {
      const created = await createDistributedRenderGroup(
        backendUrl,
        selectedMachines.map((machine) => machine.id),
        file.name
      );
      setPendingGroupId(created.group_id || "");

      await uploadDistributedRenderInput(created.upload_url, file, (pct) => {
        setUploadProgress(pct);
      });
      setFlowStage(FLOW_STAGE.UPLOADED);
    } catch (err) {
      setError(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleStartRendering = async () => {
    if (flowStage !== FLOW_STAGE.UPLOADED) {
      return;
    }
    if (!pendingGroupId) {
      setError("Upload must complete before starting render");
      return;
    }

    const frameRange = manualFrameRange;

    if (!frameRange) {
      setError("Enter a valid manual frame range before starting render");
      return;
    }
    if (cameraMode === "camera_ranges" && !cameraRangesValidation.ok) {
      setError(cameraRangesValidation.error || "Camera ranges are invalid");
      return;
    }

    setError("");
    setServerParseError("");
    setStarting(true);
    setFlowStage(FLOW_STAGE.STARTING);

    try {
      const renderOverrides = {
        scene_name: sceneName || null,
        camera_mode: cameraMode,
        camera_name: forceCameraName || null,
        view_layer: viewLayerName || null,
        camera_ranges:
          cameraMode === "camera_ranges"
            ? cameraRangesValidation.rows.map((row) => ({
                camera_name: row.camera_name,
                frame_start: row.frame_start,
                frame_end: row.frame_end,
                frame_step: row.frame_step,
                enabled: row.enabled !== false,
              }))
            : [],
        timeline: {
          frame_start: frameRange.frame_start,
          frame_end: frameRange.frame_end,
          frame_step: frameRange.frame_step || 1,
        },
      };
      const result = await confirmDistributedJob(
        backendUrl,
        pendingGroupId,
        selectedMachines.map((machine) => machine.id),
        frameRange,
        renderOverrides,
        null,
        analysisResult
      );

      if (result.needs_frame_input) {
        const detail = result.parse_error
          ? `Server parse failed: ${result.parse_error}`
          : "Server requires manual frame range";
        setServerParseError(detail);
        setError("Could not start render. Provide frame range and retry.");
        setFlowStage(FLOW_STAGE.UPLOADED);
        return;
      }

      setFlowStage(FLOW_STAGE.SUBMITTED);
      onJobSubmitted(
        result.group_id,
        file?.name || result.input_filename || "input.blend",
        result.tasks || [],
        result.total_frames
      );
    } catch (err) {
      setError(err.message || "Failed to start render");
      setFlowStage(FLOW_STAGE.UPLOADED);
    } finally {
      setStarting(false);
    }
  };

  const handleFileChange = (event) => {
    const nextFile = event.target.files?.[0] || null;
    setFile(nextFile);
    resetSubmissionFlow();
  };

  const handleCameraRangeUpdate = (rowId, patch) => {
    setCameraRanges((prev) =>
      prev.map((row) => (row.id === rowId ? { ...row, ...patch } : row))
    );
  };

  const handleCameraRangeRemove = (rowId) => {
    setCameraRanges((prev) => prev.filter((row) => row.id !== rowId));
  };

  const handleCameraRangeAdd = () => {
    const manualRange = parseManualFrameRange(frameStart, frameEnd, frameStep);
    const defaultStart = manualRange?.frame_start ?? 1;
    const defaultEnd = manualRange?.frame_end ?? defaultStart;
    const defaultStep = manualRange?.frame_step ?? 1;
    const fallbackCamera = forceCameraName || availableCameras[0] || selectedSceneInfo?.active_camera || "";
    setCameraRanges((prev) => [
      ...prev,
      {
        id: nextCameraRangeId(),
        enabled: true,
        camera_name: fallbackCamera,
        frame_start: defaultStart,
        frame_end: defaultEnd,
        frame_step: defaultStep,
      },
    ]);
  };

  const handleCameraRangesAutoFill = () => {
    if (!selectedSceneInfo) return;
    setCameraRanges(buildDefaultCameraRanges(selectedSceneInfo));
  };

  const manualFrameRange = useMemo(
    () => parseManualFrameRange(frameStart, frameEnd, frameStep),
    [frameStart, frameEnd, frameStep]
  );

  const cameraRangesValidation = useMemo(() => {
    if (cameraMode !== "camera_ranges") {
      return { ok: true, error: "", rows: [] };
    }
    return validateCameraRanges(cameraRanges, manualFrameRange);
  }, [cameraMode, cameraRanges, manualFrameRange]);

  const cameraRangeFrameCounts = useMemo(() => {
    const map = new Map();
    if (!manualFrameRange) return map;
    for (const row of cameraRanges) {
      map.set(row.id, countRowFramesWithinTimeline(row, manualFrameRange));
    }
    return map;
  }, [cameraRanges, manualFrameRange]);

  const canUpload = flowStage === FLOW_STAGE.ANALYZED && !analyzing && !uploading && !starting;
  const manualRangeValid = manualFrameRange !== null;
  const canStart =
    flowStage === FLOW_STAGE.UPLOADED &&
    !analyzing &&
    !uploading &&
    !starting &&
    Boolean(pendingGroupId) &&
    manualRangeValid &&
    (cameraMode !== "camera_ranges" || cameraRangesValidation.ok);

  const stepLabel = analyzing
    ? "Analyzing"
    : uploading
    ? "Uploading"
    : starting
    ? "Starting"
    : "";

  const currentStageLabel =
    flowStage === FLOW_STAGE.IDLE
      ? "Ready to analyze"
      : flowStage === FLOW_STAGE.ANALYZED
      ? analysisResult
        ? "Analysis complete"
        : "Manual frame range required"
      : flowStage === FLOW_STAGE.UPLOADED
      ? "Upload complete"
      : flowStage === FLOW_STAGE.STARTING
      ? "Starting render"
      : "Submitted";

  const currentStageTone =
    flowStage === FLOW_STAGE.UPLOADED || flowStage === FLOW_STAGE.SUBMITTED
      ? "success"
      : flowStage === FLOW_STAGE.ANALYZED && !analysisResult
    ? "warning"
    : "neutral";
  const hasCompletedAnalysis = flowStage !== FLOW_STAGE.IDLE && !analyzing;

  const handleSceneSelectionChange = (nextSceneName) => {
    setSceneName(nextSceneName);
    const nextScene = analyzedScenes.find((scene) => scene.name === nextSceneName);
    if (!nextScene) return;
    setForceCameraName(nextScene.active_camera || (nextScene.cameras?.[0] ?? ""));
    setViewLayerName(nextScene.view_layers?.[0] ?? "");
    setCameraRanges(buildDefaultCameraRanges(nextScene));
    if (Number.isInteger(nextScene.frame_start)) setFrameStart(String(nextScene.frame_start));
    if (Number.isInteger(nextScene.frame_end)) setFrameEnd(String(nextScene.frame_end));
    if (Number.isInteger(nextScene.frame_step) && nextScene.frame_step > 0) {
      setFrameStep(String(nextScene.frame_step));
    }
  };

  if (view === "browse") {
    return (
      <div className="page">
        <div className="page-header">
          <h2>Marketplace</h2>
          <div className="page-header-actions">
            <button className="btn btn-secondary" onClick={loadMachines} disabled={loadingMachines}>
              {loadingMachines ? "Refreshing..." : "Refresh"}
            </button>
            <button
              className="btn btn-primary"
              disabled={selectedMachines.length === 0}
              onClick={() => setView("submit")}
            >
              Continue ({selectedMachines.length})
            </button>
          </div>
        </div>

        {selectedMachines.length > 0 && (
          <div className="selection-summary">
            <span className="selection-count">{selectedMachines.length} selected</span>
            <span className="selection-hint">Frames will be split proportionally by machine power</span>
          </div>
        )}

        {machineError && <p className="error-text">{machineError}</p>}

        {loadingMachines ? (
          <div className="empty-state">
            <p>Loading machines...</p>
          </div>
        ) : machines.length === 0 ? (
          <div className="empty-state">
            <p>No machines available right now.</p>
          </div>
        ) : (
          <div className="machine-list">
            {machines.map((machine) => (
              <MachineCard
                key={machine.id}
                machine={machine}
                selected={selectedMachineIdSet.has(machine.id)}
                onToggle={handleToggleMachine}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="page">
      <button className="btn-back" onClick={() => setView("browse")}>
        {"<- Back to Marketplace"}
      </button>
      <h2>Distributed Render Submission</h2>

      <div className="card selected-machine-card">
        <SelectedMachinesSummary machines={selectedMachines} />
      </div>

      <div className="card submit-panel">
        <div className="submit-file-row">
          <div className="submit-file-main">
            {file ? (
              <>
                <div className="submit-file-name">{file.name}</div>
                <div className="submit-file-meta">
                  {(file.size / 1024 / 1024).toFixed(1)} MB
                </div>
              </>
            ) : (
              <>
                <div className="submit-file-name submit-file-empty">No input file selected</div>
                <div className="submit-file-meta">
                  Choose a `.blend` or `.zip` project bundle to continue.
                </div>
              </>
            )}
          </div>
          <div className="file-input-wrap">
            <label className="btn btn-secondary file-input-btn submit-file-btn">
              {file ? "Change File" : "Choose File"}
              <input
                type="file"
                accept=".blend,.zip"
                onChange={handleFileChange}
                style={{ display: "none" }}
              />
            </label>
          </div>
        </div>
        <p className="submit-file-hint">
          Use a single `.blend` only if textures are packed. Otherwise upload a `.zip` with the full project folder.
        </p>

        <div className="submit-stage-row">
          <span className={`submit-stage-pill ${currentStageTone}`}>{currentStageLabel}</span>
          {flowStage !== FLOW_STAGE.IDLE && (
            <button className="btn btn-secondary submit-reset-btn" type="button" onClick={() => resetSubmissionFlow()}>
              Reset
            </button>
          )}
        </div>

        <div className="submit-action-row">
          {flowStage === FLOW_STAGE.IDLE && (
            <button
              className="btn btn-primary submit-primary-btn"
              type="button"
              onClick={handleAnalyze}
              disabled={!file || selectedMachines.length === 0 || analyzing || uploading || starting}
            >
              {analyzing ? "Analyzing..." : "Analyze"}
            </button>
          )}

          {flowStage === FLOW_STAGE.ANALYZED && (
            <button
              className="btn btn-primary submit-primary-btn"
              type="button"
              onClick={handleUpload}
              disabled={!canUpload}
            >
              {uploading ? `Uploading... ${uploadProgress}%` : "Upload"}
            </button>
          )}

          {flowStage === FLOW_STAGE.UPLOADED && (
            <button
              className="btn btn-primary submit-primary-btn"
              type="button"
              onClick={handleStartRendering}
              disabled={!canStart}
            >
              {starting ? "Starting..." : "Start Rendering"}
            </button>
          )}

          {flowStage === FLOW_STAGE.STARTING && (
            <button className="btn btn-primary submit-primary-btn" type="button" disabled>
              Starting...
            </button>
          )}
        </div>

        {(analyzing || uploading || starting) && (
          <div className="runtime-progress-wrap">
            <div className={`runtime-progress-track ${uploading ? "" : "indeterminate"}`}>
              <div className="runtime-progress-fill" style={{ width: uploading ? `${uploadProgress}%` : "40%" }} />
            </div>
            <div className="runtime-progress-meta">
              <span>{stepLabel}</span>
              <span>{uploading ? `${uploadProgress}%` : "Working..."}</span>
            </div>
          </div>
        )}

        {hasCompletedAnalysis && analysisResult && (
          <div className="manual-range-panel">
            <div className="manual-range-title">Scene & Camera</div>
            <div className="manual-range-subtitle">
              Choose how camera selection is applied across frames.
            </div>
            <div className="manual-range-grid">
              <label className="manual-range-field">
                <span className="manual-range-label">Scene</span>
                <select
                  value={sceneName}
                  onChange={(event) => handleSceneSelectionChange(event.target.value)}
                  className="manual-range-input"
                >
                  {(analyzedScenes.length ? analyzedScenes : [{ name: "", label: "Default Scene" }]).map((scene) => (
                    <option key={scene.name || "default"} value={scene.name || ""}>
                      {scene.name || "Default Scene"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="manual-range-field">
                <span className="manual-range-label">Camera Mode</span>
                <select
                  value={cameraMode}
                  onChange={(event) => setCameraMode(event.target.value)}
                  className="manual-range-input"
                >
                  {CAMERA_MODE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="manual-range-field">
                <span className="manual-range-label">Force Camera</span>
                <select
                  value={forceCameraName}
                  onChange={(event) => setForceCameraName(event.target.value)}
                  className="manual-range-input"
                  disabled={cameraMode !== "force_camera" || availableCameras.length === 0}
                >
                  {availableCameras.length > 0 ? (
                    availableCameras.map((cameraName) => (
                      <option key={cameraName} value={cameraName}>
                        {cameraName}
                      </option>
                    ))
                  ) : (
                    <option value="">No cameras found</option>
                  )}
                </select>
              </label>
              <label className="manual-range-field">
                <span className="manual-range-label">View Layer</span>
                <select
                  value={viewLayerName}
                  onChange={(event) => setViewLayerName(event.target.value)}
                  className="manual-range-input"
                >
                  {availableViewLayers.length > 0 ? (
                    availableViewLayers.map((layerName) => (
                      <option key={layerName} value={layerName}>
                        {layerName}
                      </option>
                    ))
                  ) : (
                    <option value="">Default</option>
                  )}
                </select>
              </label>
            </div>
            {cameraMode === "auto_markers" && (
              <div className="manual-range-subtitle" style={{ marginTop: 8 }}>
                Auto mode follows scene camera and timeline marker cuts for per-frame camera switching.
              </div>
            )}
            {cameraMode === "camera_ranges" && (
              <div className="camera-ranges-panel">
                <div className="camera-ranges-head">
                  <div className="manual-range-subtitle camera-ranges-subtitle">
                    Edit exactly which camera is used for which frame ranges.
                  </div>
                  <div className="camera-ranges-actions">
                    <button type="button" className="btn btn-secondary" onClick={handleCameraRangesAutoFill}>
                      Use Marker Cuts
                    </button>
                    <button type="button" className="btn btn-secondary" onClick={handleCameraRangeAdd}>
                      Add Range
                    </button>
                  </div>
                </div>
                <div className="camera-ranges-table-wrap">
                  <table className="camera-ranges-table">
                    <thead>
                      <tr>
                        <th>Use</th>
                        <th>Camera</th>
                        <th>Start</th>
                        <th>End</th>
                        <th>Step</th>
                        <th>Frames</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {cameraRanges.map((row) => (
                        <tr key={row.id}>
                          <td>
                            <input
                              type="checkbox"
                              checked={row.enabled !== false}
                              onChange={(event) => handleCameraRangeUpdate(row.id, { enabled: event.target.checked })}
                            />
                          </td>
                          <td>
                            <select
                              value={row.camera_name || ""}
                              onChange={(event) => handleCameraRangeUpdate(row.id, { camera_name: event.target.value })}
                              className="manual-range-input camera-ranges-input"
                            >
                              {availableCameras.length > 0 ? (
                                availableCameras.map((cameraName) => (
                                  <option key={cameraName} value={cameraName}>
                                    {cameraName}
                                  </option>
                                ))
                              ) : (
                                <option value="">No cameras</option>
                              )}
                            </select>
                          </td>
                          <td>
                            <input
                              type="number"
                              min="1"
                              value={row.frame_start ?? ""}
                              onChange={(event) => handleCameraRangeUpdate(row.id, { frame_start: event.target.value })}
                              className="manual-range-input camera-ranges-input"
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              min="1"
                              value={row.frame_end ?? ""}
                              onChange={(event) => handleCameraRangeUpdate(row.id, { frame_end: event.target.value })}
                              className="manual-range-input camera-ranges-input"
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              min="1"
                              value={row.frame_step ?? 1}
                              onChange={(event) => handleCameraRangeUpdate(row.id, { frame_step: event.target.value })}
                              className="manual-range-input camera-ranges-input"
                            />
                          </td>
                          <td>
                            <span className="camera-ranges-count">
                              {cameraRangeFrameCounts.get(row.id) || 0}
                            </span>
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn btn-secondary camera-ranges-remove"
                              onClick={() => handleCameraRangeRemove(row.id)}
                            >
                              Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                      {cameraRanges.length === 0 && (
                        <tr>
                          <td colSpan={7} className="camera-ranges-empty">
                            No camera ranges configured. Add one to continue.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
                {manualFrameRange && (
                  <div className="manual-range-subtitle camera-ranges-subtitle">
                    Timeline has {countFramesInRange(
                      manualFrameRange.frame_start,
                      manualFrameRange.frame_end,
                      manualFrameRange.frame_step
                    )} frames after step filtering.
                  </div>
                )}
                {!cameraRangesValidation.ok && (
                  <p className="error-text camera-ranges-error">{cameraRangesValidation.error}</p>
                )}
              </div>
            )}
          </div>
        )}

        {hasCompletedAnalysis && (
          <div className="manual-range-panel">
            <div className="manual-range-title">Frame Range</div>
            <div className="manual-range-subtitle">
              {analysisResult
                ? "Auto-filled from analysis. You can edit before upload/start."
                : "Required because this file could not be parsed locally."}
            </div>
            <div className="manual-range-grid">
              <label className="manual-range-field">
                <span className="manual-range-label">Start Frame</span>
                <input
                  type="number"
                  min="1"
                  value={frameStart}
                  onChange={(event) => setFrameStart(event.target.value)}
                  className="manual-range-input"
                />
              </label>
              <label className="manual-range-field">
                <span className="manual-range-label">End Frame</span>
                <input
                  type="number"
                  min="1"
                  value={frameEnd}
                  onChange={(event) => setFrameEnd(event.target.value)}
                  className="manual-range-input"
                />
              </label>
              <label className="manual-range-field">
                <span className="manual-range-label">Frame Step</span>
                <input
                  type="number"
                  min="1"
                  value={frameStep}
                  onChange={(event) => setFrameStep(event.target.value)}
                  className="manual-range-input"
                />
              </label>
            </div>
          </div>
        )}

        {hasCompletedAnalysis && clientParseError && <p className="error-text">{clientParseError}</p>}
        {hasCompletedAnalysis && serverParseError && <p className="error-text">{serverParseError}</p>}
        {error && <p className="error-text">{error}</p>}
      </div>
    </div>
  );
}
