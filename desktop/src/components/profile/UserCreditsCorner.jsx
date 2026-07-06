import { useUserProfile } from "../../contexts/UserProfileContext";

function formatCredits(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toFixed(0);
}

/**
 * Always-visible bottom-right widget showing the user's tier and
 * credit balance.
 *
 * The balance comes from ``UserProfileContext`` which streams updates
 * via Firestore ``onSnapshot``.  Every time the server's monitor tick
 * commits a debit transaction, this widget re-renders within
 * milliseconds.  No polling, no auto-refresh timer, no manual refresh
 * button -- the value is always live.
 */
export default function UserCreditsCorner() {
  const { profile, loading } = useUserProfile();

  if (!profile && !loading) return null;

  return (
    <div className="user-credits-corner" role="status" aria-label="Tokens balance">
      {profile ? (
        <>
          <span className="user-credits-corner-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="8" cy="8" r="6" />
              <path d="M18.09 10.37A6 6 0 1 1 10.34 18" />
              <path d="M7 6h1v4" />
              <path d="m16.71 13.88.7.71-2.82 2.82" />
            </svg>
          </span>
          <span className="user-credits-corner-tier">{profile.tier || "free"}</span>
          <span className="user-credits-corner-sep">·</span>
          <span className="user-credits-corner-credits">
            {formatCredits(profile.credits)}
          </span>
          <span className="user-credits-corner-unit">tokens</span>
        </>
      ) : (
        <span className="user-credits-corner-loader">Loading…</span>
      )}
    </div>
  );
}
