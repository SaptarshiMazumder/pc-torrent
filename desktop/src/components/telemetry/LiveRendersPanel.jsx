import { formatCredits } from "../../utils/creditsFormat";

// The "Live now" strip — only rendered when something is in flight.  Totals
// only (active count, frames in flight, tokens live); the per-GPU breakdown
// lives in GpuUsagePanel so each panel keeps one concern.
export default function LiveRendersPanel({ live }) {
  const { activeRenders, activeChunks, framesInFlight, totalFramesInFlight, liveCredits } = live;
  return (
    <section className="tele-live">
      <div className="tele-live-head">
        <span className="tele-live-dot" aria-hidden="true" />
        <h3>Live now</h3>
        <span className="tele-live-count">
          {activeRenders} render{activeRenders === 1 ? "" : "s"} · {activeChunks} chunk{activeChunks === 1 ? "" : "s"}
        </span>
      </div>
      <div className="tele-live-body">
        <div className="tele-live-metric">
          <div className="tele-live-metric-value">
            {framesInFlight}
            <span className="tele-live-metric-total">/{totalFramesInFlight}</span>
          </div>
          <div className="tele-live-metric-label">frames in flight</div>
        </div>
        <div className="tele-live-metric">
          <div className="tele-live-metric-value">{formatCredits(liveCredits)}</div>
          <div className="tele-live-metric-label">tokens live</div>
        </div>
      </div>
    </section>
  );
}
