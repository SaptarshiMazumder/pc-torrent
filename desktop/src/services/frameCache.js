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

// ── In-memory layer over the disk cache ────────────────────────────────────
// The disk cache alone still costs one Tauri IPC roundtrip per lookup, so
// every remount of a thumbnail (list ↔ detail navigation, token refresh,
// poll-driven re-renders) briefly flashed blank while awaiting it — and two
// components asking for the same key concurrently double-fetched from the
// network.  The promise map dedups in-flight work; the resolved map makes
// repeat lookups synchronous for the session.
const _inflight = new Map(); // cacheKey -> Promise<assetUrl>
const _resolvedUrls = new Map(); // cacheKey -> assetUrl

/**
 * Synchronous lookup of an already-resolved preview URL for this session.
 * Lets components seed their initial <img src> without a blank-frame flash;
 * returns "" when the key hasn't been resolved yet.
 */
export function getResolvedFramePreview(cacheKey) {
  return _resolvedUrls.get(cacheKey) || "";
}

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
  const resolved = _resolvedUrls.get(cacheKey);
  if (resolved) return Promise.resolve(resolved);

  const pending = _inflight.get(cacheKey);
  if (pending) return pending;

  const p = _loadFramePreview(previewUrl, cacheKey).then((url) => {
    _resolvedUrls.set(cacheKey, url);
    _inflight.delete(cacheKey);
    return url;
  });
  // Failed loads must not poison the key — evict so a later call retries.
  p.catch(() => _inflight.delete(cacheKey));
  _inflight.set(cacheKey, p);
  return p;
}

async function _loadFramePreview(previewUrl, cacheKey) {
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
