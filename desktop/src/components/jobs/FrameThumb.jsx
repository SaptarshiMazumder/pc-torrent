import { useEffect, useRef, useState } from "react";
import { getFirebaseToken } from "../../services/api";
import { getCachedFramePreview } from "../../services/frameCache";
import { isPreviewableExtension } from "../../utils/jobUtils";
import openexrIcon from "../../assets/openexr-icon-color.svg";

function fileExtLabel(name) {
  const dot = typeof name === "string" ? name.lastIndexOf(".") : -1;
  return dot >= 0 ? name.slice(dot + 1).toUpperCase() : "FILE";
}

function isExrFilename(name) {
  return typeof name === "string" && /\.exr$/i.test(name);
}

export default function FrameThumb({ file, backendUrl, className }) {
  const [src, setSrc] = useState("");
  const mountedRef = useRef(true);
  const previewable = isPreviewableExtension(file?.filename);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    if (!previewable || !file?.preview_path || !backendUrl || !file?.filename) return;

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
        if (file.size_bytes != null) url.searchParams.set("v", String(file.size_bytes));

        return getCachedFramePreview(url.toString(), cacheKey);
      }

      try {
        const localUrl = await attempt();
        if (!cancelled && mountedRef.current) setSrc(localUrl);
      } catch {
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
  }, [previewable, file?.preview_path, file?.job_id, file?.filename, file?.size_bytes, backendUrl]);

  // Non-previewable formats (e.g. multilayer .exr) can't be browser-
  // thumbnailed; show a format badge instead of a broken/blank tile.
  if (!previewable) {
    if (isExrFilename(file?.filename)) {
      return (
        <div className={`${className} frame-thumb-exr`} title={file?.filename}>
          <img src={openexrIcon} alt="OpenEXR" className="frame-thumb-exr-icon" />
        </div>
      );
    }
    return (
      <div
        className={className}
        title={file?.filename}
        style={{ background: "var(--bg-secondary, #1a1a1a)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-secondary, #888)", fontSize: "0.7rem", fontWeight: 600, letterSpacing: "0.05em" }}
      >
        {fileExtLabel(file?.filename)}
      </div>
    );
  }

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
