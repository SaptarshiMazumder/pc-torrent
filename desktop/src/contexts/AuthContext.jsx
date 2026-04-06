import { createContext, useContext, useEffect, useState } from "react";
import {
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
} from "firebase/auth";
import { auth } from "../firebase/config";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    return onAuthStateChanged(auth, async (u) => {
      setUser(u);
      setLoading(false);
      if (u) {
        // Ensure Firestore profile exists for this user
        const backendUrl = localStorage.getItem("pcrent_backend_url") ||
          "https://pcrent-server-930713698987.asia-northeast1.run.app";
        try {
          const token = await u.getIdToken();
          await fetch(`${backendUrl}/me`, {
            headers: { Authorization: `Bearer ${token}` },
          });
        } catch {}
      }
    });
  }, []);

  const signUp = (email, password) =>
    createUserWithEmailAndPassword(auth, email, password);

  const signIn = (email, password) =>
    signInWithEmailAndPassword(auth, email, password);

  const resetPassword = (email) =>
    sendPasswordResetEmail(auth, email);

  const signOut = () => firebaseSignOut(auth);

  return (
    <AuthContext.Provider value={{ user, loading, signUp, signIn, resetPassword, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
