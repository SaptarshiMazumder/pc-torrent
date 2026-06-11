import { useEffect, useState } from "react";
import { useUserProfile } from "../../contexts/UserProfileContext";

function formatCredits(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toFixed(0);
}

const AUTO_REFRESH_MS = 5000;

/**
 * Always-visible bottom-right widget showing the user's tier and
 * credit balance with a manual refresh button.
 *
 * Auto-refreshes every ``AUTO_REFRESH_MS`` so the displayed number
 * stays close to the server-side persisted balance (which itself
 * moves on the monitor-tick cadence ~10-30s).  No client-side
 * extrapolation here -- that lives on the in-view detail-page
 * credits chip where running tasks are in scope.
 */
export default function UserCreditsCorner() {
  const { profile, loading, refetch } = useUserProfile();
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!profile) return undefined;
    const id = setInterval(() => {
      void refetch();
    }, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  }, [profile, refetch]);

  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await refetch();
    } finally {
      setRefreshing(false);
    }
  };

  if (!profile && !loading) return null;

  return (
    <div className="user-credits-corner" role="status" aria-label="Credits balance">
      {profile ? (
        <>
          <span className="user-credits-corner-tier">{profile.tier || "free"}</span>
          <span className="user-credits-corner-sep">·</span>
          <span className="user-credits-corner-credits">
            {formatCredits(profile.credits)}
          </span>
          <span className="user-credits-corner-unit">credits</span>
        </>
      ) : (
        <span className="user-credits-corner-loader">Loading…</span>
      )}
      <button
        type="button"
        className="user-credits-corner-refresh"
        onClick={handleRefresh}
        disabled={refreshing}
        title="Refresh balance"
        aria-label="Refresh balance"
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={refreshing ? "user-credits-corner-refresh-icon spin" : "user-credits-corner-refresh-icon"}
        >
          <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
          <path d="M21 3v5h-5" />
        </svg>
      </button>
    </div>
  );
}
