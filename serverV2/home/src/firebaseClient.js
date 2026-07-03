// Firebase web auth bootstrapped from the backend.  The web config is NOT
// baked into this bundle — it's served by GET /app/web-config from the
// FIREBASE_WEB_CONFIG_JSON env var, so one build works for every env.

import { initializeApp } from "firebase/app";
import {
  getAuth,
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signOut,
} from "firebase/auth";

export async function initAuth() {
  const res = await fetch("/app/web-config");
  if (!res.ok) throw new Error(`web-config fetch failed (${res.status})`);
  const cfg = await res.json();
  if (!cfg.configured) return null;
  const app = initializeApp(cfg.firebase);
  return getAuth(app);
}

export function watchAuth(auth, callback) {
  return onAuthStateChanged(auth, callback);
}

export async function googleSignIn(auth) {
  await signInWithPopup(auth, new GoogleAuthProvider());
}

export async function doSignOut(auth) {
  await signOut(auth);
}
