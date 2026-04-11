import { useState } from "react";
import { removeImage, runPreflight } from "../../services/sidecar";
import PreflightChecklist from "./PreflightChecklist";

function formatStatus(flag, whenTrue, whenFalse, whenUnknown = "Not checked") {
  if (flag === true) return whenTrue;
  if (flag === false) return whenFalse;
  return whenUnknown;
}

function formatBytes(bytes) {
  if (typeof bytes !== "number" || Number.isNaN(bytes)) return "";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(0)} MB`;
}

export default function RuntimeCard({ runtimeInfo, preflightSteps, status }) {
  const [removing, setRemoving] = useState(false);
  const preflightRunning =
    status === "checking_requirements" ||
    status === "setting_up_docker";
  const connectRunning =
    status === "downloading_image" ||
    status === "installing_image" ||
    status === "registering";
  const connected = ["connected", "rendering", "paused"].includes(status);
  const refreshDisabled = preflightRunning || connectRunning || removing || connected || status === "removing_image";

  const handleRefresh = async () => {
    try {
      await runPreflight(true);
    } catch (err) {
      console.error("Failed to run preflight:", err);
      alert(`Failed to run preflight: ${err}`);
    }
  };

  const handleRemove = async () => {
    try {
      setRemoving(true);
      await removeImage();
    } catch (err) {
      console.error("Failed to delete render image:", err);
      alert(`Failed to delete render image: ${err}`);
    } finally {
      setRemoving(false);
    }
  };

  const imageStage = runtimeInfo?.image_stage || "idle";
  const progress =
    typeof runtimeInfo?.image_progress_pct === "number"
      ? Math.max(0, Math.min(100, runtimeInfo.image_progress_pct))
      : null;
  const showProgress =
    imageStage === "downloading" ||
    imageStage === "installing" ||
    imageStage === "removing";
  const isGpuCheckRunning = status === "setting_up_docker";
  const canRemove =
    !removing &&
    runtimeInfo?.image_present === true &&
    ["disconnected", "error", "needs_reboot"].includes(status);

  return (
    <div className="card runtime-card">
      <div className="card-header-row">
        <div>
          <h3>Render Runtime</h3>
          <p className="card-subtitle">
            Local requirements, Docker state, and cached render image.
          </p>
        </div>
        <div className="runtime-actions">
          <button className="btn btn-secondary" onClick={handleRefresh} disabled={refreshDisabled}>
            {preflightRunning ? "Checking..." : "Refresh"}
          </button>
          <button className="btn btn-danger" onClick={handleRemove} disabled={!canRemove}>
            {removing ? "Deleting..." : "Delete Render Image"}
          </button>
        </div>
      </div>

      {preflightSteps && preflightSteps.length > 0 ? (
        <PreflightChecklist steps={preflightSteps} />
      ) : (
        <>
          {runtimeInfo?.awaiting_uac && (
            <div className="uac-warning">
              <strong>Action needed:</strong>{" "}
              {runtimeInfo.uac_message || "Windows will ask for permission to install software — please click Yes to continue."}
            </div>
          )}

          {status === "needs_reboot" && (
            <div className="reboot-notice">
              Your PC needs a restart to finish setup. After restarting, open the app again and setup will continue automatically.
            </div>
          )}

          <div className="runtime-grid">
            <div className="runtime-item">
              <span className="runtime-label">Requirements</span>
              <span className="runtime-value">
                {runtimeInfo?.requirements_checked
                  ? formatStatus(runtimeInfo?.requirements_ready, "Ready", "Issues found")
                  : "Checking..."}
              </span>
            </div>
            <div className="runtime-item">
              <span className="runtime-label">Docker</span>
              <span className="runtime-value">
                {!runtimeInfo?.requirements_checked
                  ? "Checking..."
                  : runtimeInfo?.docker_installed === false
                    ? "Not installed"
                    : runtimeInfo?.docker_running === true
                      ? "Running"
                      : runtimeInfo?.docker_running === false
                        ? "Installed, not running"
                        : "Checking..."}
              </span>
            </div>
            <div className="runtime-item">
              <span className="runtime-label">GPU in Docker</span>
              <span className="runtime-value">
                {!runtimeInfo?.requirements_checked
                  ? "Checking..."
                  : runtimeInfo?.docker_installed === false
                    ? "Docker required"
                    : runtimeInfo?.docker_running === false
                      ? "Start Docker"
                      : isGpuCheckRunning
                        ? "Checking..."
                        : runtimeInfo?.gpu_verified === true
                          ? "Ready"
                          : runtimeInfo?.gpu_verified === false
                            ? "CPU only"
                            : "Not checked"}
              </span>
            </div>
            <div className="runtime-item">
              <span className="runtime-label">Render Image</span>
              <span className="runtime-value">
                {imageStage === "downloading"
                  ? "Downloading"
                  : imageStage === "installing"
                    ? "Installing"
                    : imageStage === "removing"
                      ? "Removing"
                      : runtimeInfo?.image_present === true
                        ? "Installed"
                        : runtimeInfo?.image_present === false
                          ? "Missing"
                          : "Checking..."}
              </span>
            </div>
          </div>
        </>
      )}

      {runtimeInfo?.image_status && (
        <p className="runtime-message">{runtimeInfo.image_status}</p>
      )}

      {showProgress && (
        <div className="runtime-progress-wrap">
          <div className={`runtime-progress-track ${progress === null ? "indeterminate" : ""}`}>
            <div
              className="runtime-progress-fill"
              style={{ width: `${progress ?? 100}%` }}
            />
          </div>
          <div className="runtime-progress-meta">
            <span>
              {progress !== null ? `${Math.round(progress)}%` : "Working..."}
            </span>
            <span>
              {runtimeInfo?.image_downloaded_bytes && runtimeInfo?.image_total_bytes
                ? `${formatBytes(runtimeInfo.image_downloaded_bytes)} / ${formatBytes(runtimeInfo.image_total_bytes)}`
                : ""}
            </span>
          </div>
        </div>
      )}

      {runtimeInfo?.requirement_issues?.length > 0 && (
        <div className="issues-list runtime-issues">
          {runtimeInfo.requirement_issues.map((issue, index) => (
            <div key={index} className="issue-item">
              {issue}
            </div>
          ))}
        </div>
      )}

      {!canRemove && runtimeInfo?.image_present === true && (
        <p className="setting-hint">
          Disconnect the agent before deleting the local render image.
        </p>
      )}
    </div>
  );
}
