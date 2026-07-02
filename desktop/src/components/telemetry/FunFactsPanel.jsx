import { formatBytesLabel, formatDurationLabel, formatCompactNumber } from "../../utils/jobUtils";

function footageLabel(sec) {
  if (!Number.isFinite(sec) || sec <= 0) return "—";
  return sec < 60 ? `${sec.toFixed(1)} s` : formatDurationLabel(Math.round(sec));
}

// Playful lifetime tidbits.  Each tile is value / label / optional hint — the
// hint carries the "which render" context (name) so the value stays a clean
// number.
export default function FunFactsPanel({ funFacts, pixelsPushed }) {
  const facts = [
    { label: "Footage rendered", value: footageLabel(funFacts.footageSec), hint: "at 24 fps" },
    {
      label: "Biggest render",
      value: funFacts.biggestFrames ? `${funFacts.biggestFrames} frames` : "—",
      hint: funFacts.biggestName,
    },
    {
      label: "Heaviest scene",
      value: formatBytesLabel(funFacts.heaviestBytes),
      hint: funFacts.heaviestName,
    },
    { label: "Avg render time", value: formatDurationLabel(funFacts.avgDurationSec) },
    { label: "Top resolution", value: funFacts.favResolution },
    { label: "Pixels pushed", value: formatCompactNumber(pixelsPushed) },
  ];
  return (
    <div className="tele-facts-grid">
      {facts.map((fact) => (
        <div className="tele-fact" key={fact.label}>
          <div className="tele-fact-value">{fact.value}</div>
          <div className="tele-fact-label">{fact.label}</div>
          {fact.hint ? <div className="tele-fact-hint" title={fact.hint}>{fact.hint}</div> : null}
        </div>
      ))}
    </div>
  );
}
