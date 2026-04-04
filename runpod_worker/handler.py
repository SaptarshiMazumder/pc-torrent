"""
RunPod serverless handler for PC Rent rendering.

Receives a render job, runs Blender via the existing render.sh script,
streams progress back to the backend, uploads output frames, and marks
the job done/failed.

Expected input:
{
    "job_id":              str,
    "blend_url":           str,   # presigned or server URL for .blend file
    "frame_start":         int,
    "frame_end":           int,
    "frame_step":          int,
    "render_overrides_b64": str,  # base64-encoded JSON render overrides
    "backend_url":         str    # e.g. "https://your-server.com"
}
"""

import base64
import json
import logging
import math
import os
import subprocess
import tempfile
import time

import requests
import runpod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BLENDER_BIN = os.getenv("BLENDER_BIN", "/opt/blender/blender")
RENDER_SH = os.getenv("RENDER_SH", "/scripts/render.sh")
PROGRESS_SCRIPT = os.getenv("PROGRESS_SCRIPT", "/scripts/progress_handler.py")
RENDER_DRIVER_SCRIPT = os.getenv("RENDER_DRIVER_SCRIPT", "/scripts/render_driver.py")

# Push a progress update to backend at most every N seconds
PROGRESS_PUSH_INTERVAL = float(os.getenv("PROGRESS_PUSH_INTERVAL", "2"))
# Max bytes per output upload request (~24 MB)
UPLOAD_BATCH_BYTES = int(os.getenv("UPLOAD_BATCH_BYTES", str(24 * 1024 * 1024)))
UPLOAD_BATCH_FILES = int(os.getenv("UPLOAD_BATCH_FILES", "50"))
RENDER_FATAL_PATTERNS = (
    "[RENDER_DRIVER] ERROR:",
    "RuntimeError: Error: Cannot render, no camera",
    "Error: Cannot render, no camera",
    "Traceback (most recent call last):",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_blend_files(root_dir: str) -> list[str]:
    blend_files: list[str] = []
    for current_root, _, files in os.walk(root_dir):
        for name in files:
            lower_name = name.lower()
            if not lower_name.endswith(".blend"):
                continue
            if lower_name.startswith("._"):
                continue
            full_path = os.path.join(current_root, name)
            rel = os.path.relpath(full_path, root_dir).replace("\\", "/")
            if rel.startswith("__MACOSX/") or "/._" in rel:
                continue
            blend_files.append(full_path)
    blend_files.sort()
    return blend_files


def _choose_render_target_blend(
    source_filename: str,
    input_dir: str,
    blend_files: list[str],
) -> tuple[str, bool]:
    """
    Choose a deterministic render target when archive contains multiple .blend files.
    Preference:
    1) root-level .blend files only (if any exist)
    2) file stem matches uploaded filename stem
    3) larger file size
    4) shorter/lexical relative path
    5) (fallback) shallower path for non-root-only bundles
    """
    source_stem = os.path.splitext(os.path.basename(source_filename))[0].lower()

    entries = []
    for path in blend_files:
        rel = os.path.relpath(path, input_dir).replace("\\", "/")
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        depth = rel.count("/")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        entries.append(
            {
                "path": path,
                "rel": rel,
                "stem_rank": 0 if stem == source_stem else 1,
                "depth": depth,
                "size": size,
            }
        )

    root_entries = [entry for entry in entries if entry["depth"] == 0]
    pool = root_entries if root_entries else entries
    ranked = sorted(
        pool,
        key=lambda entry: (
            entry["stem_rank"],
            -entry["size"],
            entry["depth"],
            len(entry["rel"]),
            entry["rel"].lower(),
        ),
    )
    return ranked[0]["path"], bool(root_entries)

def _mark_running(backend_url: str, job_id: str):
    try:
        requests.put(
            f"{backend_url}/jobs/{job_id}/status",
            json={"status": "running"},
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Failed to mark job running: {e}")


def _push_progress(backend_url: str, job_id: str, rendered_frames: int, total_frames: int):
    try:
        requests.put(
            f"{backend_url}/jobs/{job_id}/progress",
            json={"rendered_frames": rendered_frames, "total_frames": total_frames},
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Failed to push progress: {e}")


def _upload_outputs(backend_url: str, job_id: str, output_dir: str) -> list[str]:
    """Upload rendered output files to the backend in batches. Returns list of uploaded filenames."""
    files = sorted(
        f for f in os.listdir(output_dir)
        if os.path.isfile(os.path.join(output_dir, f))
    )
    if not files:
        log.warning("No output files found after render")
        return []

    # Split into batches by size and count
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_size = 0
    for fname in files:
        fsize = os.path.getsize(os.path.join(output_dir, fname))
        if current_batch and (
            current_size + fsize > UPLOAD_BATCH_BYTES
            or len(current_batch) >= UPLOAD_BATCH_FILES
        ):
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(fname)
        current_size += fsize
    if current_batch:
        batches.append(current_batch)

    uploaded: list[str] = []
    for i, batch in enumerate(batches):
        log.info(f"Uploading batch {i + 1}/{len(batches)} ({len(batch)} files)")
        file_handles = []
        try:
            file_handles = [
                (fname, open(os.path.join(output_dir, fname), "rb"))
                for fname in batch
            ]
            resp = requests.post(
                f"{backend_url}/jobs/{job_id}/output",
                files=[("files", (fname, fh, "application/octet-stream")) for fname, fh in file_handles],
                timeout=600,
            )
            resp.raise_for_status()
            uploaded.extend(batch)
        except Exception as e:
            log.error(f"Batch {i + 1} upload failed: {e}")
            raise
        finally:
            for _, fh in file_handles:
                fh.close()

    return uploaded


def _mark_done(backend_url: str, job_id: str, output_files: list[str]):
    requests.put(
        f"{backend_url}/jobs/{job_id}/status",
        json={"status": "done", "output_files": output_files},
        timeout=15,
    ).raise_for_status()


def _mark_failed(backend_url: str, job_id: str, error: str):
    try:
        requests.put(
            f"{backend_url}/jobs/{job_id}/status",
            json={"status": "failed", "error": error},
            timeout=15,
        )
    except Exception as e:
        log.error(f"Failed to mark job failed: {e}")


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handler(job: dict) -> dict:
    inp = job["input"]

    job_id: str = inp["job_id"]
    blend_url: str = inp["blend_url"]
    frame_start: int = int(inp["frame_start"])
    frame_end: int = int(inp["frame_end"])
    frame_step: int = int(inp.get("frame_step", 1))
    render_overrides_b64: str = inp.get("render_overrides_b64", "")
    backend_url: str = inp["backend_url"].rstrip("/")

    log.info(f"Job {job_id}: frames {frame_start}-{frame_end} step {frame_step}")

    with tempfile.TemporaryDirectory() as workdir:
        input_dir = os.path.join(workdir, "input")
        output_dir = os.path.join(workdir, "output")
        os.makedirs(input_dir)
        os.makedirs(output_dir)

        # 1. Mark running
        _mark_running(backend_url, job_id)

        # 2. Download .blend (follow redirects — server URL redirects to R2)
        log.info(f"Downloading blend from {blend_url}")
        try:
            r = requests.get(blend_url, timeout=300, allow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            err = f"Failed to download blend file: {e}"
            log.error(err)
            _mark_failed(backend_url, job_id, err)
            return {"status": "failed", "error": err}

        # Save download, then extract if it's a zip
        filename = blend_url.rstrip("/").split("/")[-1]
        raw_path = os.path.join(input_dir, filename)
        with open(raw_path, "wb") as f:
            f.write(r.content)
        log.info(f"File saved: {filename} ({len(r.content) / 1024 / 1024:.1f} MB)")

        if filename.lower().endswith(".zip"):
            import zipfile as _zf
            log.info("Extracting zip archive...")
            with _zf.ZipFile(raw_path, "r") as zf:
                zf.extractall(input_dir)
            os.remove(raw_path)
            log.info(f"Extracted contents: {os.listdir(input_dir)}")

        blend_files = _find_blend_files(input_dir)
        if not blend_files:
            err = "No .blend file found in uploaded input bundle"
            log.error(err)
            _mark_failed(backend_url, job_id, err)
            return {"status": "failed", "error": err}

        blend_path, selected_from_root = _choose_render_target_blend(filename, input_dir, blend_files)
        chosen_rel = os.path.relpath(blend_path, input_dir).replace("\\", "/")
        if len(blend_files) > 1:
            candidates = sorted(
                (os.path.relpath(p, input_dir).replace("\\", "/") for p in blend_files),
                key=lambda rel: (rel.count("/"), len(rel), rel.lower()),
            )
            preview = ", ".join(candidates[:4])
            extra = "" if len(candidates) <= 4 else ", ..."
            selection_mode = "root-level priority" if selected_from_root else "fallback (no root-level .blend found)"
            log.warning(
                f"Found {len(blend_files)} .blend files in bundle. "
                f"Selected '{chosen_rel}' ({selection_mode}). Candidates: {preview}{extra}"
            )
        else:
            log.info(f"Selected render target: {chosen_rel}")

        # 3. Decode render overrides
        render_overrides: dict = {}
        if render_overrides_b64:
            try:
                render_overrides = json.loads(base64.b64decode(render_overrides_b64).decode())
            except Exception as e:
                log.warning(f"Failed to decode render_overrides_b64: {e}")

        device_policy = (
            render_overrides.get("render", {}).get("device_policy", "AUTO").upper()
        )

        # 4. Run render.sh
        env = {
            **os.environ,
            "BLENDER_BIN": BLENDER_BIN,
            "INPUT_DIR": input_dir,
            "OUTPUT_DIR": output_dir,
            "BLEND_FILE": blend_path,  # empty string = render.sh auto-discovers
            "FRAME_START": str(frame_start),
            "FRAME_END": str(frame_end),
            "FRAME_STEP": str(frame_step),
            "DEVICE_POLICY": device_policy,
            "RENDER_OVERRIDES_B64": render_overrides_b64,
            "PROGRESS_SCRIPT": PROGRESS_SCRIPT,
            "RENDER_DRIVER_SCRIPT": RENDER_DRIVER_SCRIPT,
        }

        log.info(f"Starting render: {RENDER_SH}")
        proc = subprocess.Popen(
            ["bash", RENDER_SH],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # 5. Stream progress
        total_frames = (frame_end - frame_start) // frame_step + 1
        rendered_frames = 0
        last_push = 0.0
        fatal_render_error = ""

        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log.info(line)
                if not fatal_render_error:
                    for pattern in RENDER_FATAL_PATTERNS:
                        if pattern in line:
                            fatal_render_error = line
                            break

            if "PCR_PROGRESS" in line:
                try:
                    payload = json.loads(line.split("PCR_PROGRESS", 1)[1].strip())
                    if payload.get("kind") == "frame":
                        rendered_frames = max(rendered_frames, int(payload.get("rendered_frames", 0)))
                        total_frames = max(total_frames, int(payload.get("total_frames", total_frames)))
                    elif payload.get("kind") == "meta":
                        total_frames = max(total_frames, int(payload.get("total_frames", total_frames)))
                except Exception:
                    pass

                now = time.monotonic()
                if now - last_push >= PROGRESS_PUSH_INTERVAL:
                    _push_progress(backend_url, job_id, rendered_frames, total_frames)
                    last_push = now

        proc.wait()

        if proc.returncode != 0 or fatal_render_error:
            if fatal_render_error:
                err = f"Render runtime error detected: {fatal_render_error}"
            else:
                err = f"render.sh exited with code {proc.returncode}"
            log.error(err)
            _mark_failed(backend_url, job_id, err)
            return {"status": "failed", "error": err}

        # 6. Upload outputs
        log.info("Render complete, uploading outputs")
        try:
            uploaded = _upload_outputs(backend_url, job_id, output_dir)
        except Exception as e:
            err = f"Output upload failed: {e}"
            log.error(err)
            _mark_failed(backend_url, job_id, err)
            return {"status": "failed", "error": err}
        if not uploaded:
            err = "Render produced no output files"
            log.error(err)
            _mark_failed(backend_url, job_id, err)
            return {"status": "failed", "error": err}

        # 7. Mark done
        _mark_done(backend_url, job_id, uploaded)
        log.info(f"Job {job_id} done — {len(uploaded)} files uploaded")
        return {"status": "done", "output_files": uploaded}


runpod.serverless.start({"handler": handler})
