"""Modal serverless handler for PC Rent rendering.

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

This file is the flow narrative.  Grunt work is delegated:

    BackendClient                — every HTTP call to the render backend
    ModalHeartbeat               — periodic liveness ping
    IncrementalOutputUploader    — stream frames to R2 as they appear
    EGLWatchdog                  — kill Blender on sticky OpenGL errors
    MemoryWatchdog               — kill Blender before kernel OOM-kill
    blend_file_discovery         — pick the right .blend in a bundle
    kill_process_group           — shared subprocess-termination helper

Open THIS file to understand what happens during a Modal render.
"""

import base64
import json
import logging
import os
import subprocess
import tempfile
import time

import requests

from modal_worker.backend_client import BackendClient
from modal_worker.blend_file_discovery import (
    choose_render_target,
    find_blend_files,
)
from modal_worker.egl_watchdog import EGLWatchdog, WEDGE_PATTERNS
from modal_worker.incremental_output_uploader import IncrementalOutputUploader
from modal_worker.memory_watchdog import MemoryWatchdog
from modal_worker.modal_heartbeat import ModalHeartbeat


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BLENDER_BIN = os.getenv("BLENDER_BIN", "/opt/blender/blender")
RENDER_SH = os.getenv("RENDER_SH", "/scripts/render.sh")
PROGRESS_SCRIPT = os.getenv("PROGRESS_SCRIPT", "/scripts/progress_handler.py")
RENDER_DRIVER_SCRIPT = os.getenv("RENDER_DRIVER_SCRIPT", "/scripts/render_driver.py")

# Push a progress update to the backend at most every N seconds.
PROGRESS_PUSH_INTERVAL_SEC = float(os.getenv("PROGRESS_PUSH_INTERVAL", "2"))

# EGL watchdog: grace window after first EGL/OpenGL error before we conclude
# Blender is wedged and kill it.  Transient EEVEE probe failures that Blender
# recovers from (e.g. by falling back to CYCLES) will clear the watchdog when
# the next PCR_PROGRESS event arrives.
EGL_WATCHDOG_SEC = float(os.getenv("EGL_WATCHDOG_SEC", "90"))

# Memory watchdog: kill Blender when available RAM falls below this fraction
# of total RAM, so the Python handler can return cleanly before the kernel
# OOM-kills the whole container.
MEM_WATCHDOG_MIN_FREE_FRACTION = float(os.getenv("MEM_WATCHDOG_MIN_FREE_FRACTION", "0.10"))
MEM_WATCHDOG_POLL_SEC = float(os.getenv("MEM_WATCHDOG_POLL_SEC", "1.0"))


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

    client = BackendClient(backend_url, job_id)

    # First thing: claim the worker-start.  If another container already
    # claimed this job_id, Modal re-queued us behind our backs — refuse
    # immediately, return a clean dict so Modal (with retries=NO_RETRY) does
    # not spawn yet another container.  No 5 GB blend redownload, no
    # scene-prep, no wasted compute.
    if not client.try_worker_start():
        log.error(
            "worker-start conflict: Modal re-invoked handler for job %s — "
            "refusing to re-execute",
            job_id,
        )
        return {
            "status": "failed",
            "error": "Duplicate Modal invocation refused",
        }

    with tempfile.TemporaryDirectory() as workdir:
        input_dir = os.path.join(workdir, "input")
        output_dir = os.path.join(workdir, "output")
        os.makedirs(input_dir)
        os.makedirs(output_dir)

        # 1. Mark running + start heartbeat
        client.mark_running()
        heartbeat = ModalHeartbeat(client, job_id)
        heartbeat.start(phase="downloading")

        # 2. Download .blend (follow redirects — server URL redirects to R2)
        log.info(f"Downloading blend from {blend_url}")
        try:
            r = requests.get(blend_url, timeout=300, allow_redirects=True, stream=True)
            r.raise_for_status()
        except Exception as e:
            err = f"Failed to download blend file: {e}"
            log.error(err)
            heartbeat.stop()
            client.mark_failed(err)
            return {"status": "failed", "error": err}

        # Stream download to disk to avoid loading entire file into RAM
        filename = blend_url.rstrip("/").split("/")[-1]
        raw_path = os.path.join(input_dir, filename)
        downloaded_bytes = 0
        with open(raw_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                downloaded_bytes += len(chunk)
        log.info(f"File saved: {filename} ({downloaded_bytes / 1024 / 1024:.1f} MB)")

        if filename.lower().endswith(".zip"):
            import zipfile as _zf
            log.info("Extracting zip archive...")
            with _zf.ZipFile(raw_path, "r") as zf:
                zf.extractall(input_dir)
            os.remove(raw_path)
            log.info(f"Extracted contents: {os.listdir(input_dir)}")

        heartbeat.set_phase("rendering")
        blend_files = find_blend_files(input_dir)
        if not blend_files:
            err = "No .blend file found in uploaded input bundle"
            log.error(err)
            heartbeat.stop()
            client.mark_failed(err)
            return {"status": "failed", "error": err}

        blend_path, selected_from_root = choose_render_target(filename, input_dir, blend_files)
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
            "BLEND_FILE": blend_path,
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
            start_new_session=True,
        )
        uploader = IncrementalOutputUploader(client, job_id, output_dir)
        uploader.start()
        egl_watchdog = EGLWatchdog(proc, EGL_WATCHDOG_SEC)
        egl_watchdog.start()
        mem_watchdog = MemoryWatchdog(
            proc,
            min_free_fraction=MEM_WATCHDOG_MIN_FREE_FRACTION,
            poll_sec=MEM_WATCHDOG_POLL_SEC,
        )
        mem_watchdog.start()

        # 5. Stream progress
        total_frames = (frame_end - frame_start) // frame_step + 1
        rendered_frames = 0
        last_push = 0.0

        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log.info(line)
                for pattern in WEDGE_PATTERNS:
                    if pattern in line:
                        egl_watchdog.note_wedge_pattern(line)
                        break

            if "PCR_PROGRESS" in line:
                egl_watchdog.note_progress()
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
                if now - last_push >= PROGRESS_PUSH_INTERVAL_SEC:
                    client.push_progress(rendered_frames, total_frames)
                    last_push = now

        proc.wait()
        egl_watchdog.stop()
        mem_watchdog.stop()
        heartbeat.set_phase("uploading")
        uploader.stop()
        try:
            uploader.flush_final()
        except Exception as exc:
            log.warning(f"Final incremental output flush failed: {exc}")
        uploaded = uploader.uploaded

        if proc.returncode != 0 or egl_watchdog.fired or mem_watchdog.fired:
            if mem_watchdog.fired:
                err = f"Memory watchdog killed Blender — {mem_watchdog.reason}"
            elif egl_watchdog.fired:
                err = (
                    f"EGL watchdog killed Blender after {EGL_WATCHDOG_SEC:.0f}s "
                    f"without progress ({egl_watchdog.first_error_line})"
                )
            else:
                err = f"render.sh exited with code {proc.returncode}"
            if uploaded:
                err = f"{err}. {len(uploaded)} frame(s) already uploaded and recoverable."
            log.error(err)
            heartbeat.stop()
            client.mark_failed(err)
            return {"status": "failed", "error": err, "output_files": uploaded}

        # 6. Catch up any files that were not incrementally uploaded
        local_files = sorted(
            f for f in os.listdir(output_dir)
            if os.path.isfile(os.path.join(output_dir, f))
        )
        missing_files = [f for f in local_files if f not in uploaded]
        if missing_files:
            log.info(f"Uploading remaining {len(missing_files)} output file(s)")
            try:
                catch_up = _catch_up_upload(client, output_dir, missing_files)
                uploaded += [f for f in catch_up if f not in uploaded]
            except Exception as e:
                err = f"Output catch-up upload failed: {e}"
                log.error(err)
                heartbeat.stop()
                client.mark_failed(err)
                return {"status": "failed", "error": err, "output_files": uploaded}
        if not uploaded:
            err = "Render produced no output files"
            log.error(err)
            heartbeat.stop()
            client.mark_failed(err)
            return {"status": "failed", "error": err}

        # Push a final progress snapshot based on the outputs that actually made
        # it to storage so the backend can reconcile the chunk before `done`.
        try:
            client.push_progress(max(rendered_frames, len(uploaded)), total_frames)
        except Exception:
            pass

        # 7. Finish.  Completion is decided by the orchestrator based on
        # registered output files — we do not self-declare "done".  The
        # backend will cancel this Modal function once it detects completion.
        heartbeat.stop()
        log.info(f"Job {job_id} finished rendering - {len(uploaded)} files uploaded")
        return {"status": "finished", "output_files": uploaded}


# ---------------------------------------------------------------------------
# Catch-up upload for any files the IncrementalOutputUploader missed
# ---------------------------------------------------------------------------

def _catch_up_upload(
    client: BackendClient,
    output_dir: str,
    filenames: list[str],
) -> list[str]:
    files = sorted(f for f in filenames if os.path.isfile(os.path.join(output_dir, f)))
    if not files:
        return []

    log.info(f"Requesting presigned upload URLs for {len(files)} file(s)")
    urls = client.request_upload_urls(files)

    uploaded: list[str] = []
    for i, fname in enumerate(files):
        url = urls.get(fname)
        if not url:
            raise RuntimeError(f"No presigned URL returned for {fname}")
        fpath = os.path.join(output_dir, fname)
        fsize = os.path.getsize(fpath)
        log.info(f"Uploading {i + 1}/{len(files)}: {fname} ({fsize / 1024 / 1024:.1f} MB)")
        with open(fpath, "rb") as fh:
            put_resp = requests.put(
                url,
                data=fh,
                headers={"Content-Type": "application/octet-stream"},
                timeout=600,
            )
            put_resp.raise_for_status()
        uploaded.append(fname)

    log.info(f"Registering {len(uploaded)} output file(s) with server")
    client.register_outputs(uploaded)
    return uploaded
