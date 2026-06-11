import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { getMyProfile } from "../services/api";
import { useAuth } from "./AuthContext";

const UserProfileContext = createContext(null);

function getBackendUrl() {
  return (
    localStorage.getItem("pcrent_backend_url") ||
    "https://pcrent-server-v2-930713698987.asia-northeast1.run.app"
  );
}

export function UserProfileProvider({ children }) {
  const { user } = useAuth();
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // Guards against a stale fetch (older request) overwriting a fresh
  // result when refetch() is called in quick succession.
  const fetchSeq = useRef(0);

  const refetch = useCallback(async () => {
    if (!user) return null;
    const seq = ++fetchSeq.current;
    setLoading(true);
    setError(null);
    try {
      const data = await getMyProfile(getBackendUrl());
      if (seq === fetchSeq.current) {
        setProfile(data);
      }
      return data;
    } catch (e) {
      if (seq === fetchSeq.current) {
        setError(e);
      }
      return null;
    } finally {
      if (seq === fetchSeq.current) {
        setLoading(false);
      }
    }
  }, [user]);

  useEffect(() => {
    if (!user) {
      setProfile(null);
      return;
    }
    void refetch();
  }, [user, refetch]);

  return (
    <UserProfileContext.Provider value={{ profile, loading, error, refetch }}>
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
