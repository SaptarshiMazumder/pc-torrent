import { useEffect, useRef, useState } from "react";
import { getFirebaseToken } from "../lib/api";
import { getCachedFramePreview } from "../lib/frameCache";

/**
 * Displays a frame thumbnail backed by a local JPEG cache.
 *
 * Gets its own fresh Firebase token so it's never blocked by stale
 * authToken state in the parent. Falls back to a blank tile (no crash)
 * if the fetch fails.
 */
export default function FrameThumb({ file, backendUrl, className }) {
  const [src, setSrc] = useState("");
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!file?.preview_path || !backendUrl || !file?.filename) return;

    let cancelled = false;

    async function load() {
      const cacheKey = `${file.job_id || "unknown"}/${file.filename}`;

      async function attempt() {
        const token = await getFirebaseToken();
        if (cancelled) return;

        const url = new URL(
          file.preview_path,
          backendUrl.replace(/\/+$/, "") + "/"
        );
        if (token) url.searchParams.set("token", token);
        // cache-bust by size so stale caches get replaced when the file changes
        if (file.size_bytes != null) url.searchParams.set("v", String(file.size_bytes));

        return getCachedFramePreview(url.toString(), cacheKey);
      }

      try {
        const localUrl = await attempt();
        if (!cancelled && mountedRef.current) setSrc(localUrl);
      } catch {
        // One retry after a short delay — handles transient server drops
        await new Promise((r) => setTimeout(r, 1500));
        if (cancelled) return;
        try {
          const localUrl = await attempt();
          if (!cancelled && mountedRef.current) setSrc(localUrl);
        } catch {
          // Give up silently — leave the tile blank
        }
      }
    }

    void load();
    return () => { cancelled = true; };
  }, [file?.preview_path, file?.job_id, file?.filename, file?.size_bytes, backendUrl]);

  if (!src) {
    return <div className={className} style={{ background: "var(--bg-secondary, #1a1a1a)" }} />;
  }

  return (
    <img
      className={className}
      src={src}
      alt={file.filename}
      loading="lazy"
    />
  );
}
