import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

// Deadline keys in display order.  Each key's human label + the phase it
// applies to are looked up from myJobs:stall.keys.<key>.  ``heartbeat_grace_sec``
// is intentionally absent -- it's a server-side grace window, not a
// UI-meaningful kill deadline.
const KEY_ORDER = [
  "startup_timeout_sec",
  "in_queue_timeout_sec",
  "download_phase_max_sec",
  "download_bytes_stall_sec",
  "loading_stall_sec",
  "frame_progress_stale_sec",
  "hard_ceiling_sec",
];

function fmtDuration(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "–";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  if (m < 60) return r > 0 ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

const ClockIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </svg>
);

/**
 * Drawer-style extension of the active card.  Anchored to the card via
 * ``anchorRef``: matches its left/width and sits flush against its
 * bottom edge, so visually it reads as the card "expanding" downward.
 *
 * Rendered through a portal into ``document.body`` to escape the
 * card's ``overflow: hidden`` clipping.  Position is recomputed in
 * document coordinates on mount and on resize -- both this drawer and
 * the card ride on the document, so they stay aligned during scroll
 * without any scroll listener.
 *
 * Styling intentionally uses the same CSS variables and inherited font
 * as the rest of the inst-active-card content (var(--text-primary),
 * var(--glass-border), Inter via global body font), so the drawer is
 * visually a continuation of the card.
 */
export default function AllowedStallTimesOverlay({
  anchorRef,
  ignoreClickRef,
  stallTimes,
  isLoading,
  onClose,
}) {
  const { t } = useTranslation(["myJobs", "common"]);
  const ref = useRef(null);
  const [pos, setPos] = useState({ top: -9999, left: -9999, width: 0 });
  const [accent, setAccent] = useState("");

  useLayoutEffect(() => {
    function update() {
      if (!anchorRef?.current) return;
      const el = anchorRef.current;
      const rect = el.getBoundingClientRect();
      // Document-coords (rect.* is viewport-relative; add scrollX/Y).
      // Match card width and left edge so the drawer reads as a
      // continuation of the card.  -1 overlap hides the card's bottom
      // border behind the drawer for a seamless join.
      setPos({
        top: rect.bottom - 1 + window.scrollY,
        left: rect.left + window.scrollX,
        width: rect.width,
      });
      const c = getComputedStyle(el).getPropertyValue("--inst-color").trim();
      setAccent(c || "var(--accent)");
    }
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [anchorRef]);

  useEffect(() => {
    function onDocClick(e) {
      if (ref.current && ref.current.contains(e.target)) return;
      if (ignoreClickRef?.current && ignoreClickRef.current.contains(e.target)) return;
      onClose();
    }
    function onKey(e) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose, ignoreClickRef]);

  const node = (
    <div
      ref={ref}
      style={{
        position: "absolute",
        top: pos.top,
        left: pos.left,
        width: pos.width,
        zIndex: 1000,
        boxSizing: "border-box",
        background: "var(--glass)",
        backdropFilter: "blur(16px) saturate(1.4)",
        WebkitBackdropFilter: "blur(16px) saturate(1.4)",
        border: "1px solid var(--glass-border)",
        borderTop: "none",
        borderRadius: "0 0 14px 14px",
        padding: "14px 16px",
        boxShadow: "var(--card-shadow)",
        color: "var(--text-primary)",
        fontFamily: "inherit",
        fontSize: 12,
        overflow: "hidden",
      }}
    >
      {/* Left accent stripe matching the card's --inst-color */}
      <div
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          bottom: 0,
          width: 3,
          background: accent,
          borderRadius: "0 0 0 3px",
        }}
      />
      {/* Dashed seam: signals the drawer is a sub-section of the card */}
      <div
        style={{
          position: "absolute",
          left: 12,
          right: 12,
          top: 0,
          height: 0,
          borderTop: "1px dashed var(--hair)",
        }}
      />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          marginBottom: 10,
          gap: 6,
          color: "var(--text-secondary)",
        }}
      >
        <span style={{ display: "inline-flex", color: "var(--text-muted)", opacity: 0.8 }}>
          {ClockIcon}
        </span>
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: "var(--text-secondary)",
          }}
        >
          {t("stall.title")}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common:actions.close")}
          style={{
            marginLeft: "auto",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 22,
            height: 22,
            padding: 0,
            background: "transparent",
            border: "1px solid var(--glass-border)",
            borderRadius: "50%",
            color: "var(--text-muted)",
            cursor: "pointer",
          }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="6" y1="6" x2="18" y2="18" />
            <line x1="18" y1="6" x2="6" y2="18" />
          </svg>
        </button>
      </div>

      {isLoading && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "6px 0" }}>
          {t("stall.loading")}
        </div>
      )}

      {!isLoading && stallTimes == null && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "6px 0" }}>
          {t("stall.noData")}
        </div>
      )}

      {!isLoading && stallTimes && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
            gap: "6px 10px",
          }}
        >
          {KEY_ORDER.filter((k) => stallTimes[k] != null).map((k) => {
            return (
              <div
                key={k}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                  padding: "6px 10px",
                  background: "var(--hair)",
                  borderRadius: 6,
                  minWidth: 0,
                }}
              >
                <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
                  <span style={{ color: "var(--text-primary)", fontSize: 11, fontWeight: 600 }}>
                    {t(`stall.keys.${k}.label`)}
                  </span>
                  <span style={{ color: "var(--text-muted)", fontSize: 10 }}>
                    {t(`stall.keys.${k}.when`)}
                  </span>
                </div>
                <span
                  style={{
                    fontWeight: 700,
                    fontVariantNumeric: "tabular-nums",
                    color: "var(--text-secondary)",
                    fontSize: 11,
                    whiteSpace: "nowrap",
                  }}
                >
                  {fmtDuration(Number(stallTimes[k]))}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );

  return createPortal(node, document.body);
}
