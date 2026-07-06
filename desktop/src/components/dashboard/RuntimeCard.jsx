import { useState } from "react";
import { useTranslation } from "react-i18next";
import { removeImage, runPreflight } from "../../services/sidecar";
import PreflightChecklist from "./PreflightChecklist";

function formatStatus(flag, whenTrue, whenFalse, whenUnknown) {
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
  const { t } = useTranslation(["dashboard", "common"]);
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
      alert(t("runtime.failedPreflight", { error: err }));
    }
  };

  const handleRemove = async () => {
    try {
      setRemoving(true);
      await removeImage();
    } catch (err) {
      console.error("Failed to delete render image:", err);
      alert(t("runtime.failedDelete", { error: err }));
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
          <h3>{t("runtime.title")}</h3>
          <p className="card-subtitle">
            {t("runtime.subtitle")}
          </p>
        </div>
        <div className="runtime-actions">
          <button className="btn btn-secondary" onClick={handleRefresh} disabled={refreshDisabled}>
            {preflightRunning ? t("checking") : t("common:actions.refresh")}
          </button>
          <button className="btn btn-danger" onClick={handleRemove} disabled={!canRemove}>
            {removing ? t("runtime.deleting") : t("runtime.deleteImage")}
          </button>
        </div>
      </div>

      {preflightSteps && preflightSteps.length > 0 ? (
        <PreflightChecklist steps={preflightSteps} />
      ) : (
        <>
          {runtimeInfo?.awaiting_uac && (
            <div className="uac-warning">
              <strong>{t("preflight.actionNeeded")}</strong>{" "}
              {runtimeInfo.uac_message || t("preflight.uac")}
            </div>
          )}

          {status === "needs_reboot" && (
            <div className="reboot-notice">
              {t("preflight.reboot")}
            </div>
          )}

          <div className="runtime-grid">
            <div className="runtime-item">
              <span className="runtime-label">{t("runtime.label.requirements")}</span>
              <span className="runtime-value">
                {runtimeInfo?.requirements_checked
                  ? formatStatus(runtimeInfo?.requirements_ready, t("runtime.value.ready"), t("runtime.value.issuesFound"), t("runtime.notChecked"))
                  : t("checking")}
              </span>
            </div>
            <div className="runtime-item">
              <span className="runtime-label">{t("runtime.label.docker")}</span>
              <span className="runtime-value">
                {!runtimeInfo?.requirements_checked
                  ? t("checking")
                  : runtimeInfo?.docker_installed === false
                    ? t("runtime.value.notInstalled")
                    : runtimeInfo?.docker_running === true
                      ? t("runtime.value.running")
                      : runtimeInfo?.docker_running === false
                        ? t("runtime.value.installedNotRunning")
                        : t("checking")}
              </span>
            </div>
            <div className="runtime-item">
              <span className="runtime-label">{t("runtime.label.gpuInDocker")}</span>
              <span className="runtime-value">
                {!runtimeInfo?.requirements_checked
                  ? t("checking")
                  : runtimeInfo?.docker_installed === false
                    ? t("runtime.value.dockerRequired")
                    : runtimeInfo?.docker_running === false
                      ? t("runtime.value.startDocker")
                      : isGpuCheckRunning
                        ? t("checking")
                        : runtimeInfo?.gpu_verified === true
                          ? t("runtime.value.ready")
                          : runtimeInfo?.gpu_verified === false
                            ? t("runtime.value.cpuOnly")
                            : t("runtime.notChecked")}
              </span>
            </div>
            <div className="runtime-item">
              <span className="runtime-label">{t("runtime.label.renderImage")}</span>
              <span className="runtime-value">
                {imageStage === "downloading"
                  ? t("runtime.value.downloading")
                  : imageStage === "installing"
                    ? t("runtime.value.installing")
                    : imageStage === "removing"
                      ? t("runtime.value.removing")
                      : runtimeInfo?.image_present === true
                        ? t("runtime.value.installed")
                        : runtimeInfo?.image_present === false
                          ? t("runtime.value.missing")
                          : t("checking")}
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
              {progress !== null ? t("runtime.progressPct", { pct: Math.round(progress) }) : t("runtime.working")}
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
          {t("runtime.disconnectHint")}
        </p>
      )}
    </div>
  );
}
