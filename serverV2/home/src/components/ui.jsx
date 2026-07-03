// Shared presentational pieces: stat tiles, status pills, tables,
// progress bars, and a small single-series SVG bar chart with hover
// tooltip (per the dataviz mark specs: thin bars, rounded data ends,
// 2px gaps, recessive grid, text in ink tokens).

import { useEffect, useRef, useState } from "react";

export function Tile({ label, value, hint }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

export function Pill({ tone = "neutral", children }) {
  return (
    <span className={`pill ${tone}`}>
      <span className="dot" />
      {children}
    </span>
  );
}

export function statusTone(status) {
  switch ((status || "").toLowerCase()) {
    case "done":
    case "available":
    case "running":
      return "good";
    case "pending":
    case "uploading":
    case "idle":
      return "warning";
    case "cancelled":
      return "serious";
    case "failed":
    case "offline":
      return "critical";
    default:
      return "neutral";
  }
}

export function DataTable({ columns, rows, keyFn, onRowClick, emptyText }) {
  if (!rows || rows.length === 0) {
    return <div className="empty">{emptyText || "Nothing here right now."}</div>;
  }
  return (
    <div className="tablewrap">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.num ? "num" : ""}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={keyFn ? keyFn(row) : i}
              className={onRowClick ? "clickable" : ""}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((c) => (
                <td key={c.key} className={`${c.num ? "num" : ""} ${c.mono ? "mono" : ""}`}>
                  {c.render ? c.render(row) : row[c.key] ?? "–"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Progress({ pct }) {
  const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
  return (
    <div className="progress">
      <div className="track">
        <div className="fill" style={{ width: `${clamped}%` }} />
      </div>
      <div className="pct">{clamped.toFixed(0)}%</div>
    </div>
  );
}

// Single-series bar chart (no legend needed — the title names the series).
export function BarChart({ data, height = 160, formatValue }) {
  const [hover, setHover] = useState(null);
  const wrapRef = useRef(null);
  if (!data || data.length === 0) {
    return <div className="empty">No data in this window.</div>;
  }
  const W = 720;
  const H = height;
  const padL = 8;
  const padB = 20;
  const padT = 10;
  const max = Math.max(...data.map((d) => d.value), 0.0001);
  const innerW = W - padL * 2;
  const step = innerW / data.length;
  const barW = Math.max(3, Math.min(28, step - 2)); // ≥2px surface gap
  const fmt = formatValue || ((v) => v);

  return (
    <div className="chart-wrap" ref={wrapRef}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", display: "block" }}
        onMouseLeave={() => setHover(null)}
        role="img"
      >
        {/* recessive hairline grid: baseline + midline */}
        <line x1={padL} x2={W - padL} y1={H - padB} y2={H - padB} stroke="var(--baseline)" strokeWidth="1" />
        <line x1={padL} x2={W - padL} y1={padT + (H - padB - padT) / 2} y2={padT + (H - padB - padT) / 2} stroke="var(--grid)" strokeWidth="1" />
        {data.map((d, i) => {
          const h = ((H - padB - padT) * d.value) / max;
          const x = padL + i * step + (step - barW) / 2;
          const y = H - padB - h;
          const r = Math.min(4, barW / 2, h); // rounded data end, anchored baseline
          return (
            <g key={d.label}>
              {/* oversized hit target */}
              <rect
                x={padL + i * step}
                y={padT}
                width={step}
                height={H - padB - padT}
                fill="transparent"
                onMouseEnter={() => setHover(i)}
              />
              <path
                d={`M${x},${H - padB} v${-(h - r)} q0,${-r} ${r},${-r} h${barW - 2 * r} q${r},0 ${r},${r} v${h - r} z`}
                fill="var(--series-1)"
                opacity={hover === null || hover === i ? 1 : 0.45}
                pointerEvents="none"
              />
              {(i === 0 || i === data.length - 1 || data.length <= 8) && (
                <text
                  x={padL + i * step + step / 2}
                  y={H - 6}
                  textAnchor="middle"
                  fontSize="10"
                  fill="var(--ink-muted)"
                >
                  {d.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div
          className="chart-tooltip"
          style={{
            left: `${((padL + hover * step + step / 2) / W) * 100}%`,
            top: 0,
          }}
        >
          <span className="k">{data[hover].label}</span>
          <strong>{fmt(data[hover].value)}</strong>
        </div>
      )}
    </div>
  );
}

// Poll helper — fetches now and every `ms`, pausing when the tab is hidden.
export function usePoll(fetcher, ms, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let timer = null;
    const tick = async () => {
      if (document.hidden) {
        timer = setTimeout(tick, ms);
        return;
      }
      try {
        const result = await fetcher();
        if (alive.current) {
          setData(result);
          setError(null);
        }
      } catch (e) {
        if (alive.current) setError(e);
      } finally {
        if (alive.current) {
          setLoading(false);
          timer = setTimeout(tick, ms);
        }
      }
    };
    tick();
    return () => {
      alive.current = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading };
}
