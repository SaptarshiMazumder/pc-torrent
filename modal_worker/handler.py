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

    worker_core.BackendClient            — every HTTP call to the render backend
    worker_core.HeartbeatSender          — phase + cpu/rss/bytes telemetry to /heartbeat
    worker_core.BlendDownloader          — HTTP-range-resuming blend fetch
    worker_core.IncrementalOutputUploader — stream frames to R2 as they appear
    EGLWatchdog                          — kill Blender on sticky OpenGL errors
    MemoryWatchdog                       — kill Blender before kernel OOM-kill
    blend_file_discovery                 — pick the right .blend in a bundle

Modal-specific glue (watchdogs, EGL probe, memory cap) stays in this
package.  The worker↔server protocol layer lives in worker_core/ and is
shared with the vast and community workers — same response handling,
same heartbeat shape, same retry policy.

Open THIS file to understand what happens during a Modal render.
"""

import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time

from modal_worker.blend_file_discovery import (
    choose_render_target,
    find_blend_files,
)
from modal_worker.egl_watchdog import EGLWatchdog, WEDGE_PATTERNS
from modal_worker.memory_watchdog import MemoryWatchdog
from modal_worker.process_group_killer import kill_process_group
from worker_core import (
    BackendClient,
    BlendDownloader,
    BytesProgress,
    HeartbeatSender,
    IncrementalOutputUploader,
    PhaseTracker,
    ProcessSampler,
)


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

    # Shared terminal-signal event.  Set when the orchestrator returns
    # HTTP 410 Gone on heartbeat / progress / request-upload-urls
    # (job is in a terminal status server-side).  HeartbeatSender +
    # BackendClient both signal into it; the main subprocess-read loop
    # reads it between progress lines and exits cleanly.
    terminal_event = threading.Event()

    client = BackendClient(backend_url, job_id, terminal_event=terminal_event)

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

    # C.2 -- startup status check.  If the orchestrator already considers
    # this job terminal (sibling completed it, user cancelled, prior
    # attempt marked done), exit before downloading the blend.  Saves
    # the boot+download pipeline on a Modal re-invocation that
    # try_worker_start didn't catch.
    if client.poll_cancel_status():
        log.info(
            "Job %s already terminal at startup; exiting before download",
            job_id,
        )
        return {"status": "skipped", "reason": "job already terminal"}

    with tempfile.TemporaryDirectory() as workdir:
        input_dir = os.path.join(workdir, "input")
        output_dir = os.path.join(workdir, "output")
        os.makedirs(input_dir)
        os.makedirs(output_dir)

        # 1. Mark running + start heartbeat (full telemetry via worker_core).
        # ProcessSampler walks children so once Blender spawns it
        # contributes its CPU% to the sampled value -- handler.py alone
        # idles at ~0% while the actual work happens in the subprocess.
        client.mark_running()
        phase_tracker = PhaseTracker(
            allowed=frozenset({
                "initializing", "download", "extract",
                "loading", "rendering", "uploading",
            }),
            initial="download",
        )
        bytes_progress = BytesProgress()
        heartbeat = HeartbeatSender(
            backend_url=backend_url,
            job_id=job_id,
            phase_tracker=phase_tracker,
            process_sampler=ProcessSampler(),
            bytes_progress=bytes_progress,
            terminal_event=terminal_event,
        )
        heartbeat.start()

        # 2. Download .blend via BlendDownloader -- HTTP-range resume on
        # ChunkedEncodingError / ConnectionError / Timeout, automatic zip
        # extraction, bytes_progress fed into the heartbeat payload.
        filename = blend_url.rstrip("/").split("/")[-1]
        try:
            BlendDownloader().fetch(
                url=blend_url,
                input_dir=input_dir,
                filename=filename,
                on_bytes=bytes_progress.add,
            )
        except Exception as e:
            err = f"Failed to download blend file: {e}"
            log.error(err)
            heartbeat.stop()
            client.mark_failed(err)
            return {"status": "failed", "error": err}

        heartbeat.set_phase("loading")
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

        # Output format is applied via Blender's -F CLI flag in render.sh.
        # Setting render-only formats (OPEN_EXR_MULTILAYER, FFMPEG) on the
        # file_format enum from Python is blocked in headless -b; -F sets
        # it in Blender's core before scripts run.  Default PNG.
        render_format = (render_overrides.get("output") or {}).get("file_format") or "PNG"

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
            "RENDER_FORMAT": render_format,
            "RENDER_OVERRIDES_B64": render_overrides_b64,
            "PROGRESS_SCRIPT": PROGRESS_SCRIPT,
            "RENDER_DRIVER_SCRIPT": RENDER_DRIVER_SCRIPT,
        }

        heartbeat.set_phase("rendering")
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
            # C.1 -- terminal signal from heartbeat / progress.  Orchestrator
            # already considers this job terminal; kill the subprocess and
            # bail out of the read loop.
            if terminal_event.is_set():
                log.warning(
                    "Job %s flagged terminal mid-render; killing subprocess "
                    "and exiting",
                    job_id,
                )
                kill_process_group(proc)
                break

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
        # C.1 -- if the subprocess was killed by the terminal signal,
        # exit clean.  Orchestrator is already authoritative about the
        # terminal status; skip the mark_failed branch below.
        if terminal_event.is_set():
            heartbeat.stop()
            uploader.stop()
            log.info(
                "Job %s terminal signal acknowledged; exiting cleanly",
                job_id,
            )
            return {"status": "skipped", "reason": "job already terminal"}
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

        # 6. flush_final on the uploader covered any files left after
        # the render exited (one non-stability-gated scan).  Anything still
        # missing means R2 PUTs themselves failed -- a separate retry path
        # would just hit the same failure.  Trust the uploader.
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
