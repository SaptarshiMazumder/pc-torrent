import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

// Aurora Glass chart language: 2.5px line over a vertical gradient fill,
// faint horizontal gridlines, mono axis ticks.  Tokens/credits wear the
// data-viz violet — orange stays reserved for the brand.
const VIOLET = "rgb(139, 108, 240)";
const GRID = "rgba(154, 141, 123, 0.28)";
const TICK = "#9a8d7b";

const TOOLTIP = {
  contentStyle: {
    background: "var(--bg-primary)",
    border: "1px solid var(--gbrd)",
    borderRadius: 12,
    color: "var(--text)",
    fontSize: 12,
    fontFamily: "var(--font-ui)",
    boxShadow: "var(--card-shadow)",
  },
  labelStyle: { color: "var(--muted)", fontFamily: "var(--font-mono)", fontSize: 10 },
  itemStyle: { color: VIOLET },
};

// Tokens (credits) spent per day.  Only active days are present in the data,
// so the axis stays dense instead of padding idle stretches with zeros.
export default function TokenUsageChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height={216}>
      <AreaChart data={data} margin={{ top: 8, right: 12, left: -16, bottom: 0 }}>
        <defs>
          <linearGradient id="tele-token-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={VIOLET} stopOpacity={0.28} />
            <stop offset="100%" stopColor={VIOLET} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} strokeWidth={0.6} vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ fill: TICK, fontSize: 9.5, fontFamily: "IBM Plex Mono, monospace", fontWeight: 600 }}
          axisLine={{ stroke: GRID }}
          tickLine={false}
          minTickGap={24}
        />
        <YAxis
          tick={{ fill: TICK, fontSize: 9.5, fontFamily: "IBM Plex Mono, monospace", fontWeight: 600 }}
          axisLine={false}
          tickLine={false}
          width={34}
        />
        <Tooltip {...TOOLTIP} />
        <Area
          type="monotone"
          dataKey="credits"
          name="Tokens"
          stroke={VIOLET}
          strokeWidth={2.5}
          strokeLinecap="round"
          fill="url(#tele-token-grad)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
