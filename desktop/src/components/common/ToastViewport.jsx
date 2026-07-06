import { useTranslation } from "react-i18next";
import { useToast } from "../../contexts/ToastContext";

export default function ToastViewport() {
  const { t } = useTranslation("shared");
  const { toasts, dismissToast } = useToast();
  if (!toasts.length) return null;
  return (
    <div className="toast-viewport" role="status" aria-live="polite">
      {toasts.map((t) => (
        <button
          key={t.id}
          type="button"
          className={`toast toast--${t.kind}`}
          onClick={() => dismissToast(t.id)}
          aria-label={t("actions.dismiss")}
        >
          <span className="toast-message">{t.message}</span>
        </button>
      ))}
    </div>
  );
}
