import { useTranslation } from "react-i18next";
import { useUserProfile } from "../../contexts/UserProfileContext";

function formatCredits(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toFixed(0);
}

/**
 * Header chip rendering the user's tier and credit balance.
 *
 * The balance comes from the ``UserProfileContext`` which subscribes
 * to ``users/{uid}`` in Firestore via ``onSnapshot``.  When the server's
 * monitor tick atomically debits the document, this component re-renders
 * within milliseconds -- no local extrapolation, no in-flight subtraction
 * math, no polling.
 */
export default function UserCreditsHeader() {
  const { t } = useTranslation("common");
  const { profile, loading } = useUserProfile();

  if (loading && !profile) {
    return (
      <div className="user-credits-header user-credits-header--loading">
        <span className="user-credits-header-loader">…</span>
      </div>
    );
  }
  if (!profile) return null;

  return (
    <div className="user-credits-header">
      <span className="user-credits-header-tier">{profile.tier || "free"}</span>
      <span className="user-credits-header-sep">·</span>
      <span className="user-credits-header-credits">{formatCredits(profile.credits)}</span>
      <span className="user-credits-header-unit">{t("tokens")}</span>
    </div>
  );
}
