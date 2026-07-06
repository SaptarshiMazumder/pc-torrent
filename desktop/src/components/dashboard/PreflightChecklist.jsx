import { useTranslation } from "react-i18next";
import Loader from "../common/Loader";

export default function PreflightChecklist({ steps }) {
  const { t } = useTranslation(["dashboard", "common"]);

  if (!steps || steps.length === 0) return null;

  const uacStep = steps.find((s) => s.awaiting_uac);
  // Only show reboot notice for the docker_running step specifically
  const rebootStep = steps.find(
    (s) => s.id === "docker_running" && s.status === "failed" && /restart/i.test(s.detail)
  );

  return (
    <div className="preflight-checklist">
      {uacStep && (
        <div className="uac-warning">
          <strong>{t("preflight.actionNeeded")}</strong> {t("preflight.uac")}
        </div>
      )}

      {rebootStep && (
        <div className="reboot-notice">
          {t("preflight.reboot")}
        </div>
      )}

      <div className="preflight-steps">
        {steps.map((step) => (
          <div
            key={step.id}
            className={`preflight-step preflight-step--${step.status}`}
          >
            <span className="preflight-step-icon">
              {step.status === "running" && (
                <Loader size="sm" />
              )}
              {step.status === "passed" && (
                <span className="icon-check">&#10003;</span>
              )}
              {step.status === "failed" && (
                <span className="icon-cross">&#10007;</span>
              )}
              {step.status === "warning" && (
                <span className="icon-warning">&#9888;</span>
              )}
              {step.status === "pending" && (
                <span className="icon-pending">&#9675;</span>
              )}
              {step.status === "skipped" && (
                <span className="icon-skipped">&#8212;</span>
              )}
            </span>

            <div className="preflight-step-content">
              <span className="preflight-step-name">{step.name}</span>
              {step.detail && (
                <span className="preflight-step-detail">{step.detail}</span>
              )}
              {step.status === "running" && step.progress != null && (
                <div className="preflight-step-progress">
                  <div
                    className="preflight-step-progress-fill"
                    style={{ width: `${Math.max(0, Math.min(100, step.progress))}%` }}
                  />
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
