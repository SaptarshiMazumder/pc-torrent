import { createContext, useCallback, useContext, useRef, useState } from "react";

const ToastContext = createContext(null);

const DEFAULT_TTL_MS = 4000;
const MAX_VISIBLE = 4;

let _nextId = 1;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const timersRef = useRef(new Map());

  const dismissToast = useCallback((id) => {
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const pushToast = useCallback(
    (message, kind = "info", { ttlMs = DEFAULT_TTL_MS } = {}) => {
      const id = _nextId++;
      const entry = { id, message, kind };
      setToasts((prev) => [...prev.slice(-(MAX_VISIBLE - 1)), entry]);
      const timer = setTimeout(() => {
        timersRef.current.delete(id);
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, ttlMs);
      timersRef.current.set(id, timer);
      return id;
    },
    []
  );

  return (
    <ToastContext.Provider value={{ toasts, pushToast, dismissToast }}>
      {children}
    </ToastContext.Provider>
  );
}

export const useToast = () => {
  const ctx = useContext(ToastContext);
  // Fallback no-op when the hook is used outside the provider -- safer
  // than throwing, since callers that fire toasts shouldn't crash the
  // whole tree if the provider is mounted late.
  return ctx || { toasts: [], pushToast: () => 0, dismissToast: () => {} };
};
