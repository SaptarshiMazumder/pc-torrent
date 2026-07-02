// PKCE (RFC 7636) challenge generation for the OAuth authorization-code
// flow.  Pure Web Crypto — no OAuth-provider or Tauri knowledge.

function base64UrlEncode(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

// Returns { verifier, challenge } where
// challenge = BASE64URL(SHA-256(ASCII(verifier))), per RFC 7636.
export async function createPkceChallenge() {
  const randomBytes = new Uint8Array(32);
  crypto.getRandomValues(randomBytes);
  const verifier = base64UrlEncode(randomBytes);

  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  const challenge = base64UrlEncode(new Uint8Array(digest));

  return { verifier, challenge };
}
