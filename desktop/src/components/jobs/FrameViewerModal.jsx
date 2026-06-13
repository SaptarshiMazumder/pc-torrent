import { useEffect, useRef, useState } from "react";

export default function FrameViewerModal({ viewer, onClose }) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const draggingRef = useRef(null);

  useEffect(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, [viewer?.imageSrc]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
      if (event.key === "0") {
        setScale(1);
        setOffset({ x: 0, y: 0 });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const applyZoom = (nextValue) => {
    setScale(Math.max(1, Math.min(8, nextValue)));
  };

  const onWheel = (event) => {
    event.preventDefault();
    applyZoom(scale + (event.deltaY > 0 ? -0.16 : 0.16));
  };

  const onMouseDown = (event) => {
    if (viewer?.loading || viewer?.error) return;
    draggingRef.current = { x: event.clientX, y: event.clientY };
  };

  const onMouseMove = (event) => {
    if (!draggingRef.current) return;
    const dx = event.clientX - draggingRef.current.x;
    const dy = event.clientY - draggingRef.current.y;
    draggingRef.current = { x: event.clientX, y: event.clientY };
    setOffset((prev) => ({ x: prev.x + dx, y: prev.y + dy }));
  };

  const clearDrag = () => { draggingRef.current = null; };

  return (
    <div className="frame-viewer-modal" onClick={onClose}>
      <div className="frame-viewer-card" onClick={(e) => e.stopPropagation()}>
        <div className="frame-viewer-head">
          <div>
            <strong>{viewer?.title || "Frame"}</strong>
            {viewer?.action ? (
              <span className="frame-viewer-subtext"> ({viewer.action})</span>
            ) : null}
          </div>
          <div className="frame-viewer-actions">
            <button className="btn btn-secondary" type="button" onClick={() => applyZoom(scale - 0.2)}>−</button>
            <button className="btn btn-secondary" type="button" onClick={() => applyZoom(scale + 0.2)}>+</button>
            <button className="btn btn-secondary" type="button" onClick={() => { setScale(1); setOffset({ x: 0, y: 0 }); }}>Reset</button>
            <button className="btn btn-secondary" type="button" onClick={onClose}>Close</button>
          </div>
        </div>

        {viewer?.loading ? (
          <div className="frame-viewer-status">Downloading full-resolution frame...</div>
        ) : viewer?.error ? (
          <div className="frame-viewer-status frame-viewer-error">{viewer.error}</div>
        ) : (
          <div
            className="frame-viewer-canvas"
            onWheel={onWheel}
            onMouseDown={onMouseDown}
            onMouseMove={onMouseMove}
            onMouseUp={clearDrag}
            onMouseLeave={clearDrag}
          >
            <img
              className="frame-viewer-image"
              src={viewer?.imageSrc || ""}
              alt={viewer?.title || "Rendered frame"}
              draggable={false}
              style={{
                transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
                cursor: scale > 1 ? "grab" : "default",
              }}
            />
          </div>
        )}

      </div>
    </div>
  );
}
