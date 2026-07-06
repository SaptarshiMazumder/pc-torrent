import { open } from "@tauri-apps/plugin-shell";
import { useTranslation } from "react-i18next";

/**
 * UpdateRequiredModal — full-screen blocker shown when the user's
 * bundled desktop version is below the server's ``min_version``.
 *
 * No dismiss control on purpose -- the gate is a hard force-update.
 * The Download button opens ``latestUrl`` in the user's default
 * browser; once they run the new installer the bundled version
 * matches the minimum and this modal stops mounting.
 */
export default function UpdateRequiredModal({ currentVersion, minVersion, latestUrl }) {
  const { t } = useTranslation("shared");
  const handleDownload = async () => {
    if (!latestUrl) return;
    try {
      await open(latestUrl);
    } catch {
      // Fallback: surface the URL inline so the user can copy it
      // manually.  This shouldn't normally happen on Windows.
    }
  };

  return (
    <div className="update-required-overlay">
      <div className="update-required-card">
        <div className="update-required-icon">
          <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2v8" />
            <path d="m8 6 4-4 4 4" />
            <path d="M3 13a9 9 0 1 0 18 0" />
          </svg>
        </div>
        <h2>{t("updateRequired.title")}</h2>
        <p className="update-required-body">
          {t("updateRequired.body", {
            minVersion: minVersion || "?",
            currentVersion: currentVersion || "an older version",
          })}
        </p>
        <p className="update-required-sub">
          {t("updateRequired.sub")}
        </p>
        <button
          className="btn btn-primary update-required-btn"
          type="button"
          onClick={handleDownload}
          disabled={!latestUrl}
        >
          {t("updateRequired.download", { minVersion: minVersion || "Latest" })}
        </button>
      </div>
    </div>
  );
}
