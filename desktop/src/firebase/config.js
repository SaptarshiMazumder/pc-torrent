import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: "AIzaSyBnIRWjCDnmOnOhoCEg1rgbdxVyT0W_WDg",
  authDomain: "gen-lang-client-0545494042.firebaseapp.com",
  projectId: "gen-lang-client-0545494042",
  storageBucket: "gen-lang-client-0545494042.firebasestorage.app",
  messagingSenderId: "930713698987",
  appId: "1:930713698987:web:fef33a4dd4717bde892dee",
};

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
