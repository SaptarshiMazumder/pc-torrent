import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { getFirebaseToken } from "../../services/api";
import { getCachedFramePreview, getResolvedFramePreview } from "../../services/frameCache";
import { isPreviewableExtension } from "../../utils/jobUtils";
import openexrIcon from "../../assets/openexr-icon-color.svg";

// Returns the uppercased file extension, or "" when the name has none
// (the caller substitutes a localized "FILE" fallback badge).
function fileExtLabel(name) {
  const dot = typeof name === "string" ? name.lastIndexOf(".") : -1;
  return dot >= 0 ? name.slice(dot + 1).toUpperCase() : "";
}

function isExrFilename(name) {
  return typeof name === "string" && /\.exr$/i.test(name);
}

function isTiffFilename(name) {
  return typeof name === "string" && /\.tiff?$/i.test(name);
}

export default function FrameThumb({ file, backendUrl, className }) {
  const { t } = useTranslation(["myJobs", "common"]);
  const cacheKey = file?.filename ? `${file.job_id || "unknown"}/${file.filename}` : "";
  // Seed from the session's resolved-URL map so a remounted gallery shows
  // its tiles immediately instead of flashing blank placeholders.
  const [src, setSrc] = useState(() => (cacheKey ? getResolvedFramePreview(cacheKey) : ""));
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
    if (isTiffFilename(file?.filename)) {
      return (
        <div className={`${className} frame-thumb-fmt`} title={file?.filename}>
          <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <circle cx="8.5" cy="9.5" r="1.5" />
            <path d="M21 15l-5-5-6 6-3-3-4 4" />
          </svg>
          <span className="frame-thumb-fmt-label">TIFF</span>
        </div>
      );
    }
    return (
      <div
        className={className}
        title={file?.filename}
        style={{ background: "var(--bg-secondary, #1a1a1a)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-secondary, #888)", fontSize: "0.7rem", fontWeight: 600, letterSpacing: "0.05em" }}
      >
        {fileExtLabel(file?.filename) || t("frameThumb.file")}
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
