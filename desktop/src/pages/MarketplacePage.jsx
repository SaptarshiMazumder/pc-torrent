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

  const selectedMachines = useMemo(
    () => machines.filter((machine) => selectedMachineIds.includes(machine.id)),
    [machines, selectedMachineIds]
  );
  const selectedMachineIdSet = useMemo(() => new Set(selectedMachineIds), [selectedMachineIds]);

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
    } catch (err) {
      setAnalysisResult(null);
      setClientParseError(`Client parse failed: ${err.message || "Unknown parsing error"}`);
      setFrameStart("");
      setFrameEnd("");
      setFrameStep("1");
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

    const manualRange = parseManualFrameRange(frameStart, frameEnd, frameStep);
    const frameRange = analysisResult
      ? {
          frame_start: analysisResult.frame_start,
          frame_end: analysisResult.frame_end,
          frame_step: analysisResult.frame_step || 1,
        }
      : manualRange;

    if (!frameRange) {
      setError("Enter a valid manual frame range before starting render");
      return;
    }

    setError("");
    setServerParseError("");
    setStarting(true);
    setFlowStage(FLOW_STAGE.STARTING);

    try {
      const result = await confirmDistributedJob(
        backendUrl,
        pendingGroupId,
        selectedMachines.map((machine) => machine.id),
        frameRange
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

  const canUpload = flowStage === FLOW_STAGE.ANALYZED && !analyzing && !uploading && !starting;
  const manualRangeValid = parseManualFrameRange(frameStart, frameEnd, frameStep) !== null;
  const canStart =
    flowStage === FLOW_STAGE.UPLOADED &&
    !analyzing &&
    !uploading &&
    !starting &&
    Boolean(pendingGroupId) &&
    (analysisResult ? true : manualRangeValid);

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
              disabled={!file || analyzing || uploading || starting}
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

        {flowStage !== FLOW_STAGE.IDLE && (
          <div className="submit-result-box">
            <p className="submit-result-text">
              {analysisResult
                ? `Frame range detected: ${analysisResult.frame_start}..${analysisResult.frame_end}${
                    (analysisResult.frame_step || 1) > 1
                      ? `, every ${analysisResult.frame_step} frames`
                      : ""
                  }`
                : "Analyze could not detect frames. Upload can continue, but manual frame range is required before Start Rendering."}
            </p>

            {clientParseError && <p className="error-text">{clientParseError}</p>}
            {serverParseError && <p className="error-text">{serverParseError}</p>}
          </div>
        )}

        {flowStage !== FLOW_STAGE.IDLE && !analysisResult && (
          <div className="manual-range-panel">
            <div className="manual-range-title">Manual Frame Range</div>
            <div className="manual-range-subtitle">
              Required because this file could not be parsed locally.
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

        {error && <p className="error-text">{error}</p>}
      </div>
    </div>
  );
}
