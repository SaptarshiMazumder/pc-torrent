import { useTranslation } from "react-i18next";
import { useToast } from "../../contexts/ToastContext";

export default function ToastViewport() {
  const { t } = useTranslation("shared");
  const { toasts, dismissToast } = useToast();
  if (!toasts.length) return null;
  return (
    <div className="toast-viewport" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          type="button"
          className={`toast toast--${toast.kind}`}
          onClick={() => dismissToast(toast.id)}
          aria-label={t("actions.dismiss")}
        >
          <span className="toast-message">{toast.message}</span>
        </button>
      ))}
    </div>
  );
}
