import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";
import { getFirestore } from "firebase/firestore";

// Env-driven: Vite injects import.meta.env.VITE_* at dev/build time from
// desktop/.env (committed defaults) and, when run via desktop-dev.sh /
// desktop-build.sh, the per-env desktop/.env.local override.  Firebase web
// config is PUBLIC client config (security is enforced by Firestore rules),
// so the committed defaults in desktop/.env are not secrets.
const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
};

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
// Firestore client SDK -- used by UserProfileContext's onSnapshot to
// stream live credit balance updates the moment the server's monitor
// tick commits a debit transaction.  Same project as auth; no extra
// config needed.
export const firestore = getFirestore(app);
