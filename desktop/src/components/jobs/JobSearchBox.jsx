import { useTranslation } from "react-i18next";

// Search input only.  Fully controlled by the parent (value + onChange).
export default function JobSearchBox({ value, onChange, placeholder }) {
  const { t } = useTranslation(["myJobs", "common"]);
  return (
    <div className="jobs-search-box">
      <input
        type="text"
        className="jobs-search-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || t("search.placeholderDefault")}
      />
      {value ? (
        <button
          type="button"
          className="jobs-search-clear"
          onClick={() => onChange("")}
          title={t("search.clear")}
        >
          ×
        </button>
      ) : null}
    </div>
  );
}
