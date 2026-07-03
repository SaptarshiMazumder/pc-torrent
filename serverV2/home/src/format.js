export function fmtNum(n) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString();
}

export function fmtUsd(n) {
  if (n === null || n === undefined) return "–";
  return `$${Number(n).toFixed(2)}`;
}

export function fmtCredits(n) {
  if (n === null || n === undefined) return "–";
  return Number(n).toFixed(1);
}

export function fmtPct(n) {
  if (n === null || n === undefined) return "–";
  return `${Number(n).toFixed(0)}%`;
}

export function fmtAgo(iso) {
  if (!iso) return "–";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export function fmtSecs(sec) {
  if (sec === null || sec === undefined) return "–";
  const s = Math.floor(Number(sec));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function fmtHours(sec) {
  if (sec === null || sec === undefined) return "–";
  return `${(Number(sec) / 3600).toFixed(1)}h`;
}

export function shortId(id) {
  return id ? String(id).slice(0, 8) : "–";
}

export function fmtClock(unixSec) {
  if (!unixSec) return "–";
  const d = new Date(unixSec * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

export function fmtDay(yyyymmdd) {
  const s = String(yyyymmdd);
  if (s.length === 8) return `${s.slice(4, 6)}/${s.slice(6, 8)}`;
  return s.slice(5); // ISO date -> MM-DD
}
