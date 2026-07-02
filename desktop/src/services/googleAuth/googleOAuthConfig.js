// Google OAuth 2.0 endpoints + the desktop client credentials, sourced from
// build-time env (same public-config model as firebase/config.js).
//
// The desktop client_secret is a non-confidential installed-app secret per
// RFC 8252 §8.5 — PKCE, not the secret, is what protects this flow.  Both
// values come from a "Desktop app" OAuth client in the pc-rent-dev GCP
// project; see the console setup notes.

export const GOOGLE_OAUTH = {
  authorizeEndpoint: "https://accounts.google.com/o/oauth2/v2/auth",
  tokenEndpoint: "https://oauth2.googleapis.com/token",
  scopes: ["openid", "email", "profile"],
  clientId: import.meta.env.VITE_GOOGLE_OAUTH_CLIENT_ID,
  clientSecret: import.meta.env.VITE_GOOGLE_OAUTH_CLIENT_SECRET,
};
