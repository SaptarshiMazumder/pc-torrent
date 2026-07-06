import { useTranslation } from "react-i18next";
import { useUserTelemetry } from "../hooks/useUserTelemetry";
import TelemetryOverviewHero from "../components/telemetry/TelemetryOverviewHero";
import TelemetryChartCard from "../components/telemetry/TelemetryChartCard";
import TokenUsageChart from "../components/telemetry/TokenUsageChart";
import GpuUsagePanel from "../components/telemetry/GpuUsagePanel";
import RendersLeaderboard from "../components/telemetry/RendersLeaderboard";
import LiveRendersPanel from "../components/telemetry/LiveRendersPanel";
import FunFactsPanel from "../components/telemetry/FunFactsPanel";
import Loader from "../components/common/Loader";

const EMPTY_ICON = (
  <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 3v18h18" />
    <rect x="7" y="12" width="3" height="6" rx="0.5" />
    <rect x="12" y="8" width="3" height="10" rx="0.5" />
    <rect x="17" y="4" width="3" height="14" rx="0.5" />
  </svg>
);

export default function TelemetryPage({
  ongoingJobs,
  pastJobs,
  hasMorePast,
  loadingMorePast,
  loadMorePast,
}) {
  const { lifetime, live, loadingHistory } = useUserTelemetry({
    ongoingJobs,
    pastJobs,
    hasMorePast,
    loadingMorePast,
    loadMorePast,
  });

  const isEmpty = lifetime.totalRenders === 0;
  const { t } = useTranslation("common");

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-title">
          <div className="page-eyebrow">{t("eyebrow.rentee")}</div>
          <h2>Telemetry</h2>
        </div>
        {loadingHistory && (
          <span className="tele-loading">
            <Loader size="sm" /> loading history…
          </span>
        )}
      </div>

      {isEmpty ? (
        <div className="tele-empty">
          <div className="tele-empty-mark">{EMPTY_ICON}</div>
          <h3>No renders yet</h3>
          <p>Your render stats — frames, tokens, GPUs and more — show up here once you’ve rendered something.</p>
        </div>
      ) : (
        <>
          {live.activeRenders > 0 && <LiveRendersPanel live={live} />}

          <TelemetryOverviewHero lifetime={lifetime} />

          <div className="tele-row">
            <TelemetryChartCard
              title="Token usage"
              subtitle="tokens spent per day"
              empty={lifetime.overTime.length === 0}
              emptyLabel="No spend recorded yet"
            >
              <TokenUsageChart data={lifetime.overTime} />
            </TelemetryChartCard>
            <GpuUsagePanel gpus={live.gpuUsage} />
          </div>

          <RendersLeaderboard rows={lifetime.renderRows} />

          <FunFactsPanel funFacts={lifetime.funFacts} pixelsPushed={lifetime.pixelsPushed} />
        </>
      )}
    </div>
  );
}
