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
    </div>
  );
}
