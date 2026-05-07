import { useState } from "react";

export default function SectionCard({ title, subtitle, defaultOpen = true, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="cfg-card">
      <button type="button" className="cfg-card-header" onClick={() => setOpen((v) => !v)}>
        <div className="cfg-card-title-wrap">
          <span className="cfg-card-title">{title}</span>
          {subtitle && <span className="cfg-card-subtitle">{subtitle}</span>}
        </div>
        <svg
          className={`cfg-card-chevron${open ? " open" : ""}`}
          width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2.5"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && <div className="cfg-card-body">{children}</div>}
    </div>
  );
}
