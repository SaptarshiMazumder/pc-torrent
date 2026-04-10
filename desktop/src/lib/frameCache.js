import { invoke } from "@tauri-apps/api/core";
import { convertFileSrc } from "@tauri-apps/api/core";

// ── Concurrency limiter ────────────────────────────────────────────────────
// Only MAX_CONCURRENT preview fetches run at once; the rest queue up.
// This prevents overwhelming the server when a full gallery is rendered.
const MAX_CONCURRENT = 4;
let _active = 0;
const _queue = [];

function _runNext() {
  if (_queue.length === 0 || _active >= MAX_CONCURRENT) return;
  _active++;
  const { fn, resolve, reject } = _queue.shift();
  fn().then(resolve, reject).finally(() => {
    _active--;
    _runNext();
  });
}

function _limited(fn) {
  return new Promise((resolve, reject) => {
    _queue.push({ fn, resolve, reject });
    _runNext();
  });
}
// ──────────────────────────────────────────────────────────────────────────

/**
 * Returns a local asset:// URL for the cached JPEG preview of a frame.
 * On first call for a given key it fetches the remote preview URL, converts
 * to JPEG via canvas, and writes it to the app cache dir via Rust.
 * Subsequent calls return the cached path immediately.
 *
 * @param {string} previewUrl - Authenticated URL to the server-side preview endpoint
 * @param {string} cacheKey   - Unique key for this frame, e.g. "{job_id}/{filename}"
 * @returns {Promise<string>} - asset:// URL suitable for <img src>
 */
export function getCachedFramePreview(previewUrl, cacheKey) {
  return _limited(async () => {
    // Check local disk cache first — fast path, no network
    const existing = await invoke("get_frame_cache_path", { cacheKey });
    if (existing) {
      return convertFileSrc(existing);
    }

    // Fetch the (already-resized) preview from the server
    const response = await fetch(previewUrl);
    if (!response.ok) {
      throw new Error(`Preview fetch failed: ${response.status}`);
    }
    const blob = await response.blob();

    // Convert to JPEG via canvas
    const bitmap = await createImageBitmap(blob);
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bitmap, 0, 0);
    bitmap.close();

    const jpegBlob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", 0.88)
    );
    const buffer = await jpegBlob.arrayBuffer();
    const data = Array.from(new Uint8Array(buffer));

    const savedPath = await invoke("write_frame_cache", { cacheKey, data });
    return convertFileSrc(savedPath);
  });
}
