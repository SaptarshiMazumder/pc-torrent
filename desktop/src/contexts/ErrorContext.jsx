import { createContext, useCallback, useContext, useState } from "react";
import { useTranslation } from "react-i18next";

const ErrorContext = createContext(null);

/**
 * App-level error dialog provider.
 *
 * Usage:
 *   const { showError } = useError();
 *   showError({ title: "Save failed", message: err.message, detail: err.body });
 *
 * The provider keeps a single dialog at a time -- subsequent showError calls
 * replace the visible one.  Dismissed by the user via the Close button or
 * via setError(null) externally if you need to hide it programmatically.
 */
export function ErrorProvider({ children }) {
  const { t } = useTranslation("shared");
  const [error, setError] = useState(null);

  const showError = useCallback((err) => {
    if (!err) return;
    if (typeof err === "string") {
      setError({ title: t("error.title"), message: err });
      return;
    }
    setError({
      title: err.title || t("error.title"),
      message: err.message || t("error.fallbackMessage"),
      detail: err.detail || null,
    });
  }, [t]);

  const dismiss = useCallback(() => setError(null), []);

  return (
    <ErrorContext.Provider value={{ showError, dismiss }}>
      {children}
      {error && <ErrorDialog error={error} onDismiss={dismiss} />}
    </ErrorContext.Provider>
  );
}

export function useError() {
  const ctx = useContext(ErrorContext);
  if (!ctx) {
    // Fall back to console so a missing provider doesn't crash the app.
    return {
      showError: (err) => console.error("[ErrorDialog fallback]", err),
      dismiss: () => {},
    };
  }
  return ctx;
}

function ErrorDialog({ error, onDismiss }) {
  const { t } = useTranslation("shared");
  return (
    <div className="error-dialog-backdrop" onClick={onDismiss}>
      <div
        className="error-dialog"
        role="alertdialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="error-dialog-header">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="13" />
            <line x1="12" y1="16.5" x2="12" y2="16.51" />
          </svg>
          <span className="error-dialog-title">{error.title}</span>
        </div>
        <div className="error-dialog-body">
          <p className="error-dialog-message">{error.message}</p>
          {error.detail && (
            <pre className="error-dialog-detail">{error.detail}</pre>
          )}
        </div>
        <div className="error-dialog-actions">
          <button type="button" className="btn btn-primary" onClick={onDismiss}>
            {t("actions.dismiss")}
          </button>
        </div>
      </div>
    </div>
  );
}
