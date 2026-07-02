// Titled card that frames a chart.  Owns the header + empty-state; the chart
// itself is passed as children so this stays chart-agnostic.  `wide` spans two
// columns in the charts grid for the time-series / ranking charts.
export default function TelemetryChartCard({ title, subtitle, wide, empty, emptyLabel, children }) {
  return (
    <div className={`tele-chart-card${wide ? " tele-chart-card-wide" : ""}`}>
      <div className="tele-chart-head">
        <h3 className="tele-chart-title">{title}</h3>
        {subtitle ? <span className="tele-chart-sub">{subtitle}</span> : null}
      </div>
      {empty ? (
        <div className="tele-chart-empty">{emptyLabel || "No data yet"}</div>
      ) : (
        <div className="tele-chart-body">{children}</div>
      )}
    </div>
  );
}
