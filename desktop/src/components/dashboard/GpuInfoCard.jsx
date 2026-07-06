import { useTranslation } from "react-i18next";

const SPEC_TONES = {
  gpu: "teal",
  cpu: "blue",
  ram: "violet",
  os: "green",
  driver: "amber",
};

function SpecRow({ tone, label, value }) {
  return (
    <div className="dash-spec-row">
      <span className={`dash-spec-dot dash-spec-dot--${tone}`} />
      <span className="dash-spec-label">{label}</span>
      <span className="dash-spec-value">{value}</span>
    </div>
  );
}

export default function GpuInfoCard({ systemInfo }) {
  const { t } = useTranslation(["dashboard", "common"]);

  if (!systemInfo) {
    return (
      <div className="card gpu-card">
        <div className="dash-panel-head">
          <span className="dash-panel-title">{t("system.title")}</span>
        </div>
        <p className="muted">{t("system.connectPrompt")}</p>
      </div>
    );
  }

  return (
    <div className="card gpu-card">
      <div className="dash-panel-head">
        <span className="dash-panel-title">{t("system.title")}</span>
      </div>
      <div className="dash-spec-rows">
        <SpecRow
          tone={SPEC_TONES.gpu}
          label={t("system.gpu")}
          value={
            <>
              {systemInfo.gpu_name || t("system.notDetected")}
              {systemInfo.gpu_vram_gb > 0 && (
                <span className="info-sub"> {t("system.vram", { vram: systemInfo.gpu_vram_gb })}</span>
              )}
            </>
          }
        />
        <SpecRow tone={SPEC_TONES.cpu} label={t("system.cpu")} value={t("system.cores", { cores: systemInfo.cpu_cores })} />
        <SpecRow tone={SPEC_TONES.ram} label={t("system.ram")} value={t("system.ramValue", { ram: systemInfo.ram_gb })} />
        <SpecRow tone={SPEC_TONES.os} label={t("system.os")} value={systemInfo.os_version || t("system.unknown")} />
        <SpecRow tone={SPEC_TONES.driver} label={t("system.driver")} value={systemInfo.nvidia_driver || t("system.na")} />
      </div>
      {systemInfo.issues && systemInfo.issues.length > 0 && (
        <div className="issues-list">
          {systemInfo.issues.map((issue, i) => (
            <div key={i} className="issue-item">{issue}</div>
          ))}
        </div>
      )}
    </div>
  );
}
