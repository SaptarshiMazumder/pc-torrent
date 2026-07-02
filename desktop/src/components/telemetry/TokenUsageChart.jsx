import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

const TOOLTIP = {
  contentStyle: {
    background: "#1c1f25",
    border: "1px solid #252830",
    borderRadius: 8,
    color: "#eae9f1",
    fontSize: 12,
  },
  labelStyle: { color: "#a3a4ac" },
  itemStyle: { color: "#f59e0b" },
};

// Tokens (credits) spent per day.  Only active days are present in the data,
// so the axis stays dense instead of padding idle stretches with zeros.
export default function TokenUsageChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height={216}>
      <AreaChart data={data} margin={{ top: 8, right: 12, left: -16, bottom: 0 }}>
        <defs>
          <linearGradient id="tele-token-grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.45} />
            <stop offset="100%" stopColor="#f59e0b" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#252830" vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ fill: "#5e616a", fontSize: 11 }}
          axisLine={{ stroke: "#252830" }}
          tickLine={false}
          minTickGap={24}
        />
        <YAxis
          tick={{ fill: "#5e616a", fontSize: 11 }}
          axisLine={false}
          tickLine={false}
          width={34}
        />
        <Tooltip {...TOOLTIP} />
        <Area
          type="monotone"
          dataKey="credits"
          name="Tokens"
          stroke="#f59e0b"
          strokeWidth={2}
          fill="url(#tele-token-grad)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
