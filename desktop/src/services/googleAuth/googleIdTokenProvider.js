import { invoke } from "@tauri-apps/api/core";

import { GOOGLE_OAUTH } from "./googleOAuthConfig";
import { createPkceChallenge } from "./pkce";

// Upper bound on how long the native side waits for the browser redirect.
const TIMEOUT_SECS = 300;

// Random, URL-safe anti-CSRF state echoed back by Google on the redirect.
function createStateToken() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// Drives the system-browser OAuth code + PKCE flow via the native loopback
// command and returns a Google-issued OpenID Connect ID token.  Owns the
// Google specifics + PKCE; has no Firebase knowledge — the caller trades the
// ID token for a Firebase session.
export async function acquireGoogleIdToken() {
  if (!GOOGLE_OAUTH.clientId || !GOOGLE_OAUTH.clientSecret) {
    throw new Error("Google sign-in is not configured.");
  }

  const { verifier, challenge } = await createPkceChallenge();
  const state = createStateToken();

  // The native command opens the system browser, captures the loopback
  // redirect, and returns Google's raw token response.
  const { tokenResponse } = await invoke("run_loopback_oauth", {
    request: {
      authorizeEndpoint: GOOGLE_OAUTH.authorizeEndpoint,
      authorizeParams: {
        client_id: GOOGLE_OAUTH.clientId,
        response_type: "code",
        scope: GOOGLE_OAUTH.scopes.join(" "),
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
        prompt: "select_account",
      },
      tokenEndpoint: GOOGLE_OAUTH.tokenEndpoint,
      tokenParams: {
        client_id: GOOGLE_OAUTH.clientId,
        client_secret: GOOGLE_OAUTH.clientSecret,
        grant_type: "authorization_code",
        code_verifier: verifier,
      },
      expectedState: state,
      timeoutSecs: TIMEOUT_SECS,
    },
  });

  const idToken = tokenResponse?.id_token;
  if (!idToken) throw new Error("Google did not return an ID token.");
  return idToken;
}

// Aborts an in-flight acquireGoogleIdToken (user closed/abandoned the browser):
// the native capture loop returns promptly so the pending call rejects and the
// UI can reset for a fresh attempt.
export function cancelGoogleSignIn() {
  return invoke("cancel_loopback_oauth");
}
