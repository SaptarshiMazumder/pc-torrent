import { createContext, useContext, useEffect, useState } from "react";
import { doc, onSnapshot } from "firebase/firestore";
import { firestore } from "../firebase/config";
import { getMyProfile } from "../services/api";
import { useAuth } from "./AuthContext";

const UserProfileContext = createContext(null);

function getBackendUrl() {
  return (
    localStorage.getItem("pcrent_backend_url") ||
    "https://pcrent-server-v2-930713698987.asia-northeast1.run.app"
  );
}

/**
 * Live user profile context.
 *
 * On sign-in:
 *   1. Fires a one-shot ``GET /me`` so the server creates the Firestore
 *      doc on first login (and so we have ``tier`` / ``email`` before
 *      Firestore returns).
 *   2. Opens an ``onSnapshot`` listener on ``users/{uid}``.  Every time
 *      the server's monitor tick atomically debits the doc via
 *      ``UserProfileRepository.record_spend``, this listener fires
 *      within milliseconds and React re-renders.
 *
 * No polling.  No 5s auto-refresh.  No client-side multiplier math.
 * The displayed ``credits`` value is whatever Firestore last said.
 */
export function UserProfileProvider({ children }) {
  const { user } = useAuth();
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!user) {
      setProfile(null);
      setLoading(false);
      setError(null);
      return undefined;
    }

    let unsub = () => {};
    let cancelled = false;
    setLoading(true);
    setError(null);

    (async () => {
      // One-shot /me ensures the Firestore doc exists.  We don't need
      // the response body -- the snapshot listener below will pick up
      // the document content as soon as the server's create_if_missing
      // commits.
      try {
        await getMyProfile(getBackendUrl());
      } catch (e) {
        if (!cancelled) setError(e);
      }
      if (cancelled) return;

      unsub = onSnapshot(
        doc(firestore, "users", user.uid),
        (snap) => {
          if (cancelled) return;
          if (!snap.exists()) {
            setProfile(null);
          } else {
            const data = snap.data() || {};
            setProfile({
              uid: user.uid,
              email: data.email || user.email || "",
              display_name: data.display_name || "",
              tier: data.tier || "free",
              credits: typeof data.credits === "number" ? data.credits : 0,
            });
          }
          setLoading(false);
        },
        (e) => {
          if (cancelled) return;
          setError(e);
          setLoading(false);
        },
      );
    })();

    return () => {
      cancelled = true;
      unsub();
    };
  }, [user]);

  return (
    <UserProfileContext.Provider value={{ profile, loading, error }}>
      {children}
    </UserProfileContext.Provider>
  );
}

export function useUserProfile() {
  const ctx = useContext(UserProfileContext);
  if (!ctx) {
    throw new Error("useUserProfile must be used inside UserProfileProvider");
  }
  return ctx;
}
