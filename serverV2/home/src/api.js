// Same-origin API client.  The token provider is set once by the auth
// layer; every request carries a fresh Firebase ID token.

let tokenProvider = null;

export function setTokenProvider(fn) {
  tokenProvider = fn;
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = tokenProvider ? await tokenProvider() : null;
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = body?.detail || body?.error || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

// EventSource can't set headers; the server accepts ?token= instead.
export async function sseUrl(path) {
  const token = tokenProvider ? await tokenProvider() : null;
  const url = new URL(path, window.location.origin);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}
