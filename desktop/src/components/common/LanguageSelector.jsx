import { useTranslation } from "react-i18next";
import { setLanguage, SUPPORTED_LANGUAGES } from "../../i18n/i18n";

// Compact segmented language switch for the sidebar footer.  Persists the
// choice via ``setLanguage``; ``useTranslation`` re-renders on change.
export default function LanguageSelector() {
  const { t, i18n } = useTranslation("common");
  const current = i18n.language?.startsWith("ja") ? "ja" : "en";

  return (
    <div className="sidebar-language" role="group" aria-label={t("language.label")}>
      {SUPPORTED_LANGUAGES.map((code) => (
        <button
          key={code}
          type="button"
          className={`sidebar-language-option${current === code ? " selected" : ""}`}
          onClick={() => setLanguage(code)}
          aria-pressed={current === code}
        >
          {t(`language.${code}`)}
        </button>
      ))}
    </div>
  );
}
