import { useEffect, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";

/**
 * AboutPage — read-only "what am I running" surface.  Pulls the version
 * straight from Tauri so it's always the bundled value, never a
 * hand-maintained string that could drift out of sync.
 */
export default function AboutPage() {
  const [version, setVersion] = useState("");

  useEffect(() => {
    let cancelled = false;
    getVersion()
      .then((v) => { if (!cancelled) setVersion(v); })
      .catch(() => { if (!cancelled) setVersion("unknown"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="page about-page">
      <div className="about-card">
        <h1 className="about-title">Forge</h1>
        <div className="about-version">v{version || "…"}</div>
      </div>
    </div>
  );
}
