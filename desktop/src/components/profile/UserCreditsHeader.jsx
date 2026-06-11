import { useUserProfile } from "../../contexts/UserProfileContext";
import { liveActualCost, useLiveCostTick } from "../../hooks/useLiveCostTick";

function formatCredits(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toFixed(0);
}

/**
 * Header chip rendering the user's tier and live credit balance.
 *
 * The persisted balance in ``profile.credits`` reflects the latest
 * monitor-tick debit on the server -- it lags reality by up to one
 * monitor cycle.  We close that gap locally:
 *
 *   displayed = persisted - Σ (live cost per running task × rate)
 *
 * ``liveActualCost`` is the same formula the per-task cost chips use,
 * so the credits display ticks at the same cadence and stays
 * consistent with what the user already sees on the task cards.
 *
 * @param {{runningTasks: Array}} props
 */
export default function UserCreditsHeader({ runningTasks = [] }) {
  // 1Hz signal so the displayed value re-renders even though
  // ``liveActualCost`` is closed-form and stateless.
  useLiveCostTick();
  const { profile, loading } = useUserProfile();

  if (loading && !profile) {
    return (
      <div className="user-credits-header user-credits-header--loading">
        <span className="user-credits-header-loader">…</span>
      </div>
    );
  }
  if (!profile) return null;

  const now = new Date();
  const rate = typeof profile.credits_per_usd === "number"
    ? profile.credits_per_usd
    : 0;
  let inFlightCredits = 0;
  for (const task of runningTasks) {
    const usd = liveActualCost(task, now);
    if (typeof usd === "number" && usd > 0) {
      inFlightCredits += usd * rate;
    }
  }
  const displayed = (profile.credits ?? 0) - inFlightCredits;

  return (
    <div className="user-credits-header" title={`Persisted: ${formatCredits(profile.credits)} · In-flight: ${formatCredits(inFlightCredits)}`}>
      <span className="user-credits-header-tier">{profile.tier || "free"}</span>
      <span className="user-credits-header-sep">·</span>
      <span className="user-credits-header-credits">{formatCredits(displayed)}</span>
      <span className="user-credits-header-unit">credits</span>
    </div>
  );
}
