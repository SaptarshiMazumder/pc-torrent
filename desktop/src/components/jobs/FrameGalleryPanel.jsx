import { useEffect, useRef, useState } from "react";
import FrameThumb from "./FrameThumb";

const FRAME_BATCH_SIZE = 2;

export default function FrameGalleryPanel({ id, files, loading, error, openingFrameKey, onOpenFrame, backendUrl }) {
  const [visibleCount, setVisibleCount] = useState(FRAME_BATCH_SIZE);
  const sentinelRef = useRef(null);

  useEffect(() => {
    setVisibleCount(FRAME_BATCH_SIZE);
  }, [id, files.length]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || visibleCount >= files.length) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisibleCount((n) => Math.min(n + FRAME_BATCH_SIZE, files.length));
        }
      },
      { rootMargin: "100px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [visibleCount, files.length]);

  const visibleFiles = files.slice(0, visibleCount);
  const hasMore = visibleCount < files.length;

  return (
    <div className="job-frame-gallery">
      <div className="job-frame-gallery-head">
        <span>Frames</span>
        <span className="muted">{files.length} available</span>
      </div>

      {loading && files.length === 0 && (
        <div className="job-frame-gallery-empty">Loading frames...</div>
      )}
      {!loading && !error && files.length === 0 && (
        <div className="job-frame-gallery-empty">No frames available yet.</div>
      )}
      {error && (
        <div className="rentee-job-error job-frame-gallery-error">{error}</div>
      )}

      {visibleFiles.length > 0 && (
        <div className="job-frame-grid">
          {visibleFiles.map((file) => {
            const fileKey = `${id}:${file.job_id || ""}:${file.filename}`;
            const isOpening = openingFrameKey === fileKey;
            return (
              <button
                key={fileKey}
                className="job-frame-tile"
                type="button"
                disabled={isOpening}
                title={file.filename}
                onClick={() => onOpenFrame(file)}
              >
                <FrameThumb
                  className="job-frame-thumb"
                  file={file}
                  backendUrl={backendUrl}
                />
                <span className="job-frame-name">
                  {isOpening ? "Caching..." : file.filename}
                </span>
              </button>
            );
          })}
        </div>
      )}

      {hasMore && <div ref={sentinelRef} style={{ height: 1 }} />}
    </div>
  );
}
