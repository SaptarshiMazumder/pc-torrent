import { createContext, useContext, useEffect, useState } from "react";
import {
  browserLocalPersistence,
  browserSessionPersistence,
  createUserWithEmailAndPassword,
  GoogleAuthProvider,
  onAuthStateChanged,
  sendPasswordResetEmail,
  setPersistence,
  signInWithCredential,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
} from "firebase/auth";
import { auth } from "../firebase/config";
import { acquireGoogleIdToken, cancelGoogleSignIn } from "../services/googleAuth/googleIdTokenProvider";

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
      unsub = onAuthStateChanged(auth, (u) => {
        setUser(u);
        setLoading(false);
        // UserProfileProvider fetches /me when ``user`` becomes
        // non-null -- that single fetch both creates the Firestore
        // profile (if missing) and seeds the credits display.
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

  const signInWithGoogle = async (options = {}) => {
    const remember = typeof options?.remember === "boolean" ? options.remember : rememberMe;
    setRememberMePreference(remember);
    await applyPersistence(remember);
    // The provider drives the system-browser OAuth flow and returns a Google
    // ID token; Firebase turns it into the same session as email/password.
    const idToken = await acquireGoogleIdToken();
    const credential = GoogleAuthProvider.credential(idToken);
    return signInWithCredential(auth, credential);
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
        signInWithGoogle,
        cancelGoogleSignIn,
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
