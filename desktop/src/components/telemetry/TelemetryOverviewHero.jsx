import TelemetryGauge from "./TelemetryGauge";
import { formatCredits } from "../../utils/creditsFormat";
import { formatBytesLabel, formatDurationLabel, formatCompactNumber } from "../../utils/jobUtils";

// The one compact hero card that replaces the old chunky stat-card row.
// Left: three gradient orbs for the headline totals.  Right: an engine-share
// gauge + a slim secondary strip — dense, not attention-hogging.
export default function TelemetryOverviewHero({ lifetime }) {
  const { totalRenders, framesRendered, creditsSpent, engineSplit, outputFiles, sceneBytes, funFacts } = lifetime;
  const primaryEngine = engineSplit[0] || null;
  const engineShare = primaryEngine && totalRenders ? primaryEngine.value / totalRenders : 0;

  return (
    <section className="tele-hero">
      <div className="tele-hero-orbs">
        <TelemetryGauge id="g-renders" value={totalRenders.toLocaleString()} label="renders" from="#e8724a" to="#f5a623" />
        <TelemetryGauge id="g-frames" value={formatCompactNumber(framesRendered)} label="frames" from="#5ea0fa" to="#7c5cfc" />
        <TelemetryGauge id="g-tokens" value={formatCredits(creditsSpent)} label="tokens" from="#f59e0b" to="#e8724a" />
      </div>

      <div className="tele-hero-side">
        {primaryEngine && (
          <TelemetryGauge
            id="g-engine"
            size={104}
            fillPct={engineShare}
            from="#22c55e"
            to="#5ea0fa"
            value={`${Math.round(engineShare * 100)}%`}
            label={primaryEngine.name}
          />
        )}
        <ul className="tele-hero-strip">
          <li><b>{outputFiles.toLocaleString()}</b><span>output files</span></li>
          <li><b>{formatBytesLabel(sceneBytes)}</b><span>scene data</span></li>
          <li><b>{formatDurationLabel(funFacts.avgDurationSec)}</b><span>avg render</span></li>
          <li><b>{funFacts.favResolution}</b><span>top resolution</span></li>
        </ul>
      </div>
    </section>
  );
}
