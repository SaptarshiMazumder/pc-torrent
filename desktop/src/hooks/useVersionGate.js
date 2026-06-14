import { useEffect, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";

/**
 * useVersionGate — boot-time version check against the server's
 * /app/min-version endpoint.  Compares the bundled Tauri version
 * against the live Firestore minimum and reports the result so App
 * can render UpdateRequiredModal when the user is on an old build.
 *
 * Returns:
 *   { status: "checking" | "ok" | "blocked",
 *     currentVersion: string | null,
 *     minVersion: string | null,
 *     latestUrl: string | null }
 *
 * On any backend / network error we resolve to "ok" -- we don't
 * punish users for a server outage by locking them out of the app.
 * The next launch retries the check.
 */
export function useVersionGate(backendUrl) {
  const [state, setState] = useState({
    status: "checking",
    currentVersion: null,
    minVersion: null,
    latestUrl: null,
  });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      let currentVersion = null;
      try {
        currentVersion = await getVersion();
      } catch {
        currentVersion = null;
      }

      let payload = null;
      try {
        const res = await fetch(`${backendUrl}/app/min-version`);
        if (res.ok) payload = await res.json();
      } catch {
        payload = null;
      }
      if (cancelled) return;

      const minVersion = payload?.min_version || null;
      const latestUrl = payload?.latest_url || null;

      if (!currentVersion || !minVersion) {
        setState({ status: "ok", currentVersion, minVersion, latestUrl });
        return;
      }

      const blocked = compareSemver(currentVersion, minVersion) < 0;
      setState({
        status: blocked ? "blocked" : "ok",
        currentVersion,
        minVersion,
        latestUrl,
      });
    })();
    return () => { cancelled = true; };
  }, [backendUrl]);

  return state;
}

/**
 * compareSemver — returns -1 / 0 / 1 for a vs b on dot-separated
 * numeric versions ("1.0.10" > "1.0.9").  Non-numeric segments fall
 * back to string compare for that slot.  Missing trailing segments
 * are treated as 0 so "1.0" == "1.0.0".
 */
function compareSemver(a, b) {
  const pa = String(a).split(".");
  const pb = String(b).split(".");
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i += 1) {
    const ra = pa[i] ?? "0";
    const rb = pb[i] ?? "0";
    const na = Number.parseInt(ra, 10);
    const nb = Number.parseInt(rb, 10);
    if (Number.isNaN(na) || Number.isNaN(nb)) {
      if (ra < rb) return -1;
      if (ra > rb) return 1;
      continue;
    }
    if (na < nb) return -1;
    if (na > nb) return 1;
  }
  return 0;
}
