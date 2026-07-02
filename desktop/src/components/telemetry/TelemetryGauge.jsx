// A circular gradient gauge with content in the hole — the reference's ring
// badges.  `fillPct` (0–1) draws a partial arc for real ratios (engine share);
// left at 1 it's a full decorative ring behind a headline number.  `id` must
// be unique per gauge on the page (it names the SVG gradient).
export default function TelemetryGauge({
  id,
  size = 92,
  strokeWidth = 8,
  fillPct = 1,
  from = "#e8724a",
  to = "#f5a623",
  value,
  label,
}) {
  const r = (size - strokeWidth) / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(1, Number(fillPct) || 0));
  const center = size / 2;
  return (
    <div className="tele-gauge" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor={from} />
            <stop offset="100%" stopColor={to} />
          </linearGradient>
        </defs>
        <circle cx={center} cy={center} r={r} fill="none" stroke="var(--border)" strokeWidth={strokeWidth} />
        <circle
          cx={center}
          cy={center}
          r={r}
          fill="none"
          stroke={`url(#${id})`}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - pct)}
          transform={`rotate(-90 ${center} ${center})`}
        />
      </svg>
      <div className="tele-gauge-center">
        <span className="tele-gauge-value">{value}</span>
        {label ? <span className="tele-gauge-label">{label}</span> : null}
      </div>
    </div>
  );
}
