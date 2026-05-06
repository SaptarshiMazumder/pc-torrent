import { createContext, useContext, useEffect, useState } from "react";
import {
  browserLocalPersistence,
  browserSessionPersistence,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  sendPasswordResetEmail,
  setPersistence,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
} from "firebase/auth";
import { auth } from "../firebase/config";

const AuthContext = createContext(null);
const REMEMBER_ME_KEY = "pcrent_remember_me";

function readRememberPreference() {
  try {
    const raw = localStorage.getItem(REMEMBER_ME_KEY);
    if (raw === "0") return false;
    if (raw === "1") return true;
  } catch {}
  return true;
}

function writeRememberPreference(value) {
  try {
    localStorage.setItem(REMEMBER_ME_KEY, value ? "1" : "0");
  } catch {}
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [rememberMe, setRememberMe] = useState(() => readRememberPreference());

  const applyPersistence = async (remember) => {
    await setPersistence(auth, remember ? browserLocalPersistence : browserSessionPersistence);
  };

  const setRememberMePreference = (remember) => {
    const next = !!remember;
    setRememberMe(next);
    writeRememberPreference(next);
  };

  useEffect(() => {
    let unsub = () => {};
    let cancelled = false;

    (async () => {
      try {
        await applyPersistence(readRememberPreference());
      } catch {}
      if (cancelled) return;
      unsub = onAuthStateChanged(auth, async (u) => {
        setUser(u);
        setLoading(false);
        if (u) {
          // Ensure Firestore profile exists for this user
          const backendUrl = localStorage.getItem("pcrent_backend_url") ||
            "https://pcrent-server-v2-930713698987.asia-northeast1.run.app";
          try {
            const token = await u.getIdToken();
            await fetch(`${backendUrl}/me`, {
              headers: { Authorization: `Bearer ${token}` },
            });
          } catch {}
        }
      });
    })();

    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  const signUp = async (email, password, options = {}) => {
    const remember = typeof options?.remember === "boolean" ? options.remember : rememberMe;
    setRememberMePreference(remember);
    await applyPersistence(remember);
    return createUserWithEmailAndPassword(auth, email, password);
  };

  const signIn = async (email, password, options = {}) => {
    const remember = typeof options?.remember === "boolean" ? options.remember : rememberMe;
    setRememberMePreference(remember);
    await applyPersistence(remember);
    return signInWithEmailAndPassword(auth, email, password);
  };

  const resetPassword = (email) =>
    sendPasswordResetEmail(auth, email);

  const signOut = () => firebaseSignOut(auth);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        signUp,
        signIn,
        resetPassword,
        signOut,
        rememberMe,
        setRememberMePreference,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
