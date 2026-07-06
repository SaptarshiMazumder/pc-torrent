import { useTranslation } from "react-i18next";

// List/grid view switcher.  Controlled: value ("table" | "grid") + onChange.
export default function JobsViewToggle({ value, onChange }) {
  const { t } = useTranslation(["myJobs", "common"]);
  return (
    <div className="jobs-view-toggle" role="group" aria-label={t("view.mode")}>
      <button
        type="button"
        className={`jobs-view-btn${value === "table" ? " active" : ""}`}
        onClick={() => onChange("table")}
        title={t("view.list")}
        aria-pressed={value === "table"}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path
            d="M2 4h12M2 8h12M2 12h12"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>
      </button>
      <button
        type="button"
        className={`jobs-view-btn${value === "grid" ? " active" : ""}`}
        onClick={() => onChange("grid")}
        title={t("view.grid")}
        aria-pressed={value === "grid"}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <rect x="2" y="2" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" />
          <rect x="9" y="2" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" />
          <rect x="2" y="9" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" />
          <rect x="9" y="9" width="5" height="5" rx="1" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      </button>
    </div>
  );
}
