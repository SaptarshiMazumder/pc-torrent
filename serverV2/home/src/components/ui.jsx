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
// Cross-mount cache so returning to a tab shows its last data INSTANTLY
// (stale-while-revalidate) instead of a fresh loading flash.  Panels unmount
// on tab switch, so their poll state is otherwise lost.  An in-memory Map
// keeps switches allocation-free; a sessionStorage mirror makes a full page
// reload instant too (cleared when the browser tab closes, so never long-stale).
const _cacheMem = new Map();

function cacheGet(key) {
  if (key == null) return undefined;
  if (_cacheMem.has(key)) return _cacheMem.get(key);
  try {
    const raw = sessionStorage.getItem(`pollcache:${key}`);
    if (raw != null) {
      const val = JSON.parse(raw);
      _cacheMem.set(key, val);
      return val;
    }
  } catch {
    /* sessionStorage unavailable / bad JSON — treat as miss */
  }
  return undefined;
}

function cacheSet(key, value) {
  if (key == null) return;
  _cacheMem.set(key, value);
  try {
    sessionStorage.setItem(`pollcache:${key}`, JSON.stringify(value));
  } catch {
    /* quota or serialization failure — the in-memory copy still serves */
  }
}

// Poll `fetcher` now and every `ms`, pausing when the tab is hidden.
// Pass a stable `cacheKey` to enable stale-while-revalidate across mounts:
// the last successful payload is shown immediately on return while a fresh
// fetch runs in the background.  For parameterized panels (e.g. cost window)
// vary the key with the param AND list the param in `deps`.
export function usePoll(fetcher, ms, deps = [], cacheKey = null) {
  const seed = cacheGet(cacheKey);
  const [data, setData] = useState(seed === undefined ? null : seed);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(seed === undefined);
  const [updatedAt, setUpdatedAt] = useState(null);
  const alive = useRef(true);
  const doFetchRef = useRef(null);

  useEffect(() => {
    alive.current = true;
    // Re-seed from cache for the current key — handles a key change (cost
    // window switch) without flashing the previous key's data or a null.
    const cached = cacheGet(cacheKey);
    if (cached === undefined) {
      setData(null);
      setLoading(true);
    } else {
      setData(cached);
      setLoading(false);
    }
    let timer = null;
    const doFetch = async () => {
      try {
        const result = await fetcher();
        if (alive.current) {
          cacheSet(cacheKey, result);
          setData(result);
          setError(null);
          setUpdatedAt(Date.now());
        }
      } catch (e) {
        if (alive.current) setError(e);
      } finally {
        if (alive.current) setLoading(false);
      }
    };
    // Exposed so a Refresh button can force an immediate fetch off-cadence.
    doFetchRef.current = () => { if (alive.current) { setLoading(true); doFetch(); } };
    const tick = async () => {
      if (document.hidden) {
        timer = setTimeout(tick, ms);
        return;
      }
      await doFetch();
      if (alive.current) timer = setTimeout(tick, ms);
    };
    tick();
    return () => {
      alive.current = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const refresh = () => { if (doFetchRef.current) doFetchRef.current(); };
  return { data, error, loading, updatedAt, refresh };
}

// "updated Xm ago · auto every Nm" + a manual Refresh button.  For non-live
// panels that slow-poll (costs, GCP, users, history) so they don't hammer the
// backend/external APIs — the button fetches on demand when you want fresh.
export function RefreshBar({ updatedAt, loading, onRefresh, intervalMs }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 30000);
    return () => clearInterval(t);
  }, []);
  let ago = "not loaded";
  if (updatedAt) {
    const m = Math.max(0, Math.round((Date.now() - updatedAt) / 60000));
    ago = m === 0 ? "updated just now" : `updated ${m}m ago`;
  }
  const every = intervalMs ? ` · auto every ${Math.round(intervalMs / 60000)}m` : "";
  return (
    <span className="refreshbar">
      <span className="hint">{ago}{every}</span>
      <button className="btn ghost small" onClick={onRefresh} disabled={loading}>
        {loading ? "refreshing…" : "refresh"}
      </button>
    </span>
  );
}
