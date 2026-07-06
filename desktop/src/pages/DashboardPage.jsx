import { Trans, useTranslation } from "react-i18next";
import GpuInfoCard from "../components/dashboard/GpuInfoCard";
import ConnectButton from "../components/dashboard/ConnectButton";
import StatusIndicator from "../components/common/StatusIndicator";
import JobCard from "../components/dashboard/JobCard";
import RuntimeCard from "../components/dashboard/RuntimeCard";

const STATUS_LABEL_KEYS = {
  disconnected: "common:status.disconnected",
  connected: "common:status.connected",
  rendering: "common:status.rendering",
  paused: "common:status.paused",
  error: "common:status.error",
  checking_requirements: "kpiStatus.checking",
  setting_up_docker: "kpiStatus.settingUp",
  downloading_image: "kpiStatus.downloading",
  installing_image: "kpiStatus.installing",
  registering: "kpiStatus.registering",
  removing_image: "kpiStatus.removing",
  needs_reboot: "kpiStatus.reboot",
};

function KpiIconStatus() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="14" rx="2" />
      <path d="M3 9h18" />
    </svg>
  );
}

function KpiIconGpu() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <rect x="9" y="9" width="6" height="6" />
    </svg>
  );
}

function KpiIconCpu() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2.5" />
      <rect x="9" y="9" width="6" height="6" rx="1" />
      <path d="M9 2v2M15 2v2M9 20v2M15 20v2M20 9h2M20 14h2M2 9h2M2 14h2" />
    </svg>
  );
}

function KpiIconRam() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <ellipse cx="12" cy="6" rx="8" ry="3" />
      <path d="M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6" />
      <path d="M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" />
    </svg>
  );
}

function KpiCard({ tone, Icon, label, value, suffix }) {
  return (
    <div className={`dash-kpi dash-kpi--${tone}`}>
      <div className="dash-kpi-head">
        <span className="dash-kpi-icon"><Icon /></span>
        <span className="dash-kpi-label">{label}</span>
      </div>
      <div className="dash-kpi-value-row">
        <span className="dash-kpi-value">{value}</span>
        {suffix ? <span className="dash-kpi-suffix">{suffix}</span> : null}
      </div>
    </div>
  );
}

export default function DashboardPage({
  status,
  message,
  machineId,
  systemInfo,
  runtimeInfo,
  preflightSteps,
  currentJob,
  backendUrl,
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const statusLabel = t(STATUS_LABEL_KEYS[status] || "kpiStatus.settingUp");

  return (
    <div className="page dashboard-page">
      <div className="page-eyebrow">{t("common:eyebrow.home")}</div>
      <h2>{t("title")}</h2>

      {/* KPI strip — real machine facts in the prototype's stat-card shells */}
      <div className="dash-kpis">
        <KpiCard tone="blue" Icon={KpiIconStatus} label={t("kpi.status")} value={statusLabel} />
        <KpiCard
          tone="teal"
          Icon={KpiIconGpu}
          label={t("kpi.gpuVram")}
          value={systemInfo?.gpu_vram_gb > 0 ? systemInfo.gpu_vram_gb : "—"}
          suffix={systemInfo?.gpu_vram_gb > 0 ? "GB" : ""}
        />
        <KpiCard
          tone="violet"
          Icon={KpiIconCpu}
          label={t("kpi.cpuCores")}
          value={systemInfo?.cpu_cores ?? "—"}
        />
        <KpiCard
          tone="amber"
          Icon={KpiIconRam}
          label={t("kpi.memory")}
          value={systemInfo?.ram_gb ?? "—"}
          suffix={systemInfo?.ram_gb ? "GB" : ""}
        />
      </div>

      {/* Hero row — live render panel + machine/system column */}
      <div className="dash-hero-row">
        <JobCard currentJob={currentJob} status={status} />

        <div className="dash-side">
          <div className="card dash-connect-card">
            <div className="dash-panel-head">
              <span className="dash-panel-title">{t("machine")}</span>
              <StatusIndicator status={status} message={message} />
            </div>
            <div className="connect-section">
              <ConnectButton status={status} backendUrl={backendUrl} runtimeInfo={runtimeInfo} />
              {machineId && (
                <div className="machine-id">
                  <Trans t={t} i18nKey="machineId" values={{ id: machineId }} components={{ code: <code /> }} />
                </div>
              )}
            </div>
          </div>

          <GpuInfoCard systemInfo={systemInfo} />
        </div>
      </div>

      <RuntimeCard runtimeInfo={runtimeInfo} preflightSteps={preflightSteps} status={status} />
    </div>
  );
}
