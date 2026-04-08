import { invoke } from "@tauri-apps/api/core";
import { convertFileSrc } from "@tauri-apps/api/core";

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
  // Check local disk cache first
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

  // Convert to JPEG via OffscreenCanvas / regular canvas
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
}
