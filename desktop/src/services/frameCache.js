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
export async function getCachedFramePreview(previewUrl, cacheKey) {
  // Disk hit check runs OUTSIDE the limiter — it's a cheap IPC roundtrip
  // and gating it behind MAX_CONCURRENT meant a fully-cached gallery had
  // to serialize all its disk lookups behind the network throttle, even
  // though no network was involved.  Cache hits now resolve in parallel.
  const existing = await invoke("get_frame_cache_path", { cacheKey });
  if (existing) {
    return convertFileSrc(existing);
  }

  // Cache miss -- network fetch + canvas convert + disk write.  This is
  // the expensive path; throttle it so a cold gallery doesn't stampede
  // the server.
  return _limited(async () => {
    // Re-check the disk inside the limiter: another concurrent caller
    // may have populated the cache while we were queued.
    const raced = await invoke("get_frame_cache_path", { cacheKey });
    if (raced) {
      return convertFileSrc(raced);
    }

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
