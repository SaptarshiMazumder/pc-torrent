import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import FrameThumb from "./FrameThumb";
import Loader from "../common/Loader";

const FRAME_BATCH_SIZE = 4;

// Same regex shape as serverV2's ``frame_number`` generated column:
// match the literal ``frame####.`` token anywhere in the filename.
function parseFrameNumber(filename) {
  const m = filename && filename.match(/frame(\d+)\./);
  return m ? parseInt(m[1], 10) : null;
}

// Bonus File Output node files use the worker's convention:
// ``<NodeName>_<SlotIdx>_frame####.<ext>``.  The main per-camera
// render uses ``<CameraName>_frame####.<ext>`` (no ``_<digit>_``
// segment).  That lets us sort main first inside each frame group.
function isBonusOutput(filename) {
  return /_\d+_frame\d+\./.test(filename || "");
}

function groupFilesByFrame(files) {
  const groups = new Map();
  for (const file of files) {
    const fn = parseFrameNumber(file.filename);
    const key = fn !== null ? fn : -1;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(file);
  }
  const sorted = [...groups.entries()].sort(([a], [b]) => {
    if (a === -1) return 1;
    if (b === -1) return -1;
    return a - b;
  });
  for (const [, list] of sorted) {
    list.sort((a, b) => {
      const aBonus = isBonusOutput(a.filename) ? 1 : 0;
      const bBonus = isBonusOutput(b.filename) ? 1 : 0;
      if (aBonus !== bBonus) return aBonus - bBonus;
      return (a.filename || "").localeCompare(b.filename || "");
    });
  }
  return sorted;
}

function FrameTile({ file, id, openingFrameKey, onOpenFrame, backendUrl }) {
  const { t } = useTranslation(["myJobs", "common"]);
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
      <FrameThumb className="job-frame-thumb" file={file} backendUrl={backendUrl} />
      <span className="job-frame-name">
        {isOpening ? t("frames.caching") : file.filename}
      </span>
    </button>
  );
}

export default function FrameGalleryPanel({ id, files, loading, error, openingFrameKey, onOpenFrame, backendUrl }) {
  const { t } = useTranslation(["myJobs", "common"]);
  const grouped = useMemo(() => groupFilesByFrame(files), [files]);
  // When every frame has at most one output file, fall back to the
  // flat grid -- the per-frame group headers add visual weight that
  // only pays off when each header tells you what's grouped underneath.
  const useGroupedView = useMemo(
    () => grouped.some(([, list]) => list.length > 1),
    [grouped]
  );
  const totalUnits = useGroupedView ? grouped.length : files.length;
  const [visibleCount, setVisibleCount] = useState(FRAME_BATCH_SIZE);
  const sentinelRef = useRef(null);

  useEffect(() => {
    setVisibleCount(FRAME_BATCH_SIZE);
  }, [id, totalUnits, useGroupedView]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || visibleCount >= totalUnits) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisibleCount((n) => Math.min(n + FRAME_BATCH_SIZE, totalUnits));
        }
      },
      { rootMargin: "0px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [visibleCount, totalUnits]);

  const hasMore = visibleCount < totalUnits;

  return (
    <div className="job-frame-gallery">
      {loading && files.length === 0 && (
        <div className="job-frame-gallery-empty">
          <Loader />
        </div>
      )}
      {!loading && !error && files.length === 0 && (
        <div className="job-frame-gallery-empty">{t("frames.empty")}</div>
      )}
      {error && (
        <div className="inst-error" style={{ margin: 0 }}>{error}</div>
      )}

      {totalUnits > 0 && useGroupedView && (
        <div className="job-frame-groups">
          {grouped.slice(0, visibleCount).map(([frameNum, groupFiles]) => (
            <div key={`group-${frameNum}`} className="job-frame-group">
              <div className="job-frame-group-header">
                <span className="job-frame-group-label">
                  {frameNum >= 0 ? t("frames.frameN", { n: frameNum }) : t("frames.unparsed")}
                </span>
                {groupFiles.length > 1 && (
                  <span className="job-frame-group-count">{t("frames.fileCount", { count: groupFiles.length })}</span>
                )}
              </div>
              <div className="job-frame-group-row">
                {groupFiles.map((file) => (
                  <FrameTile
                    key={`${id}:${file.job_id || ""}:${file.filename}`}
                    file={file}
                    id={id}
                    openingFrameKey={openingFrameKey}
                    onOpenFrame={onOpenFrame}
                    backendUrl={backendUrl}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {totalUnits > 0 && !useGroupedView && (
        <div className="job-frame-grid">
          {files.slice(0, visibleCount).map((file) => (
            <FrameTile
              key={`${id}:${file.job_id || ""}:${file.filename}`}
              file={file}
              id={id}
              openingFrameKey={openingFrameKey}
              onOpenFrame={onOpenFrame}
              backendUrl={backendUrl}
            />
          ))}
        </div>
      )}

      {hasMore && <div ref={sentinelRef} style={{ height: 1 }} />}
    </div>
  );
}
