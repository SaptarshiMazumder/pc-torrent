"""
Vast.ai worker handler for PC Rent rendering.

Reads job parameters from environment variables (set when the Vast.ai
instance was created), runs the Blender render, and reports back to the
backend server. The process exits when rendering is complete or fails —
the Vast.ai instance is then destroyed by the server's polling thread.

Required env vars:
    JOB_ID
    BLEND_URL
    FRAME_START
    FRAME_END
    FRAME_STEP
    RENDER_OVERRIDES_B64
    BACKEND_URL
"""

import base64
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import requests

from workflow.download import BlendDownloader
from workflow.heartbeat import (
    BytesProgress,
    HeartbeatSender,
    PhaseTracker,
    ProcessSampler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BLENDER_BIN = os.getenv("BLENDER_BIN", "/opt/blender/blender")
RENDER_SH = os.getenv("RENDER_SH", "/scripts/render.sh")
PROGRESS_SCRIPT = os.getenv("PROGRESS_SCRIPT", "/scripts/progress_handler.py")
RENDER_DRIVER_SCRIPT = os.getenv("RENDER_DRIVER_SCRIPT", "/scripts/render_driver.py")

PROGRESS_PUSH_INTERVAL = float(os.getenv("PROGRESS_PUSH_INTERVAL", "2"))
OUTPUT_SCAN_INTERVAL = float(os.getenv("OUTPUT_SCAN_INTERVAL", "1.0"))
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "10"))
# Grace window after first EGL/OpenGL error before we conclude Blender is
# wedged and kill it.  Transient EEVEE probe failures clear the watchdog when
# the next PCR_PROGRESS event arrives.
EGL_WATCHDOG_SEC = float(os.getenv("EGL_WATCHDOG_SEC", "90"))
RENDER_FATAL_PATTERNS = (
    "[RENDER_DRIVER] ERROR:",
    "RuntimeError: Error: Cannot render, no camera",
    "Error: Cannot render, no camera",
)
RENDER_WEDGE_PATTERNS = (
    "EGL_BAD_MATCH",
    "EGL_BAD_DISPLAY",
    "EGL_NOT_INITIALIZED",
    "Failed to create OpenGL context",
)


# ---------------------------------------------------------------------------
# EGL watchdog (identical to modal_worker/handler.py)
# ---------------------------------------------------------------------------

def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the bash process and every descendant (Blender, render_driver, ...).

    `proc.kill()` only signals bash itself, leaving grandchildren alive and
    holding the stdout pipe open, which wedges the handler's read loop.
    Popen must be created with `start_new_session=True` so `proc.pid` is the
    process-group id we can target with killpg.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.warning(f"Could not resolve process group for pid {proc.pid}: {exc}")
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            log.warning(f"killpg({pgid}, SIGKILL) failed: {exc}; falling back to proc.kill()")

    try:
        proc.kill()
    except Exception as exc:
        log.warning(f"proc.kill() fallback failed: {exc}")


class EGLWatchdog:
    """Kill Blender only if EGL/OpenGL errors persist without render progress.

    Blender can emit a burst of EGL errors during EEVEE probing and still
    recover (e.g. by falling back to CYCLES). Killing on the first occurrence
    aborts otherwise-healthy renders. Instead we arm a grace timer when the
    first wedge pattern appears; any PCR_PROGRESS event disarms it. If the
    timer expires with no progress the container is genuinely stuck, so we
    kill the Blender process ourselves.
    """

    def __init__(self, proc: subprocess.Popen, window_sec: float):
        self._proc = proc
        self._window_sec = window_sec
        self._lock = threading.Lock()
        self._first_error_at: float | None = None
        self._first_error_line: str = ""
        self._stop = threading.Event()
        self._fired = False
        self._thread: threading.Thread | None = None

    @property
    def fired(self) -> bool:
        return self._fired

    @property
    def first_error_line(self) -> str:
        return self._first_error_line

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="egl-watchdog"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def note_wedge_pattern(self, line: str) -> None:
        with self._lock:
            if self._first_error_at is not None:
                return
            self._first_error_at = time.monotonic()
            self._first_error_line = line
        log.warning(
            "EGL/OpenGL wedge pattern detected, arming %.0fs watchdog: %s",
            self._window_sec,
            line,
        )

    def note_progress(self) -> None:
        with self._lock:
            if self._first_error_at is None:
                return
            self._first_error_at = None
            self._first_error_line = ""
        log.info("Render progress received — disarming EGL watchdog")

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                if self._first_error_at is None:
                    continue
                elapsed = time.monotonic() - self._first_error_at
                if elapsed < self._window_sec:
                    continue
                line = self._first_error_line
            log.error(
                "EGL watchdog firing: no render progress for %.0fs after '%s', "
                "killing Blender process group",
                elapsed,
                line,
            )
            self._fired = True
            _kill_process_group(self._proc)
            return


# ---------------------------------------------------------------------------
# Helpers (identical to modal_worker/handler.py)
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
        entries.append({"path": path, "rel": rel, "stem_rank": 0 if stem == source_stem else 1, "depth": depth, "size": size})

    root_entries = [e for e in entries if e["depth"] == 0]
    pool = root_entries if root_entries else entries
    ranked = sorted(pool, key=lambda e: (e["stem_rank"], -e["size"], e["depth"], len(e["rel"]), e["rel"].lower()))
    return ranked[0]["path"], bool(root_entries)


def _mark_running(backend_url: str, job_id: str):
    try:
        requests.put(f"{backend_url}/jobs/{job_id}/status", json={"status": "running"}, timeout=15)
    except Exception as e:
        log.warning(f"Failed to mark job running: {e}")


def _push_progress(backend_url: str, job_id: str, rendered_frames: int, total_frames: int):
    try:
        requests.put(f"{backend_url}/jobs/{job_id}/progress", json={"rendered_frames": rendered_frames, "total_frames": total_frames}, timeout=15)
    except Exception as e:
        log.warning(f"Failed to push progress: {e}")


def _upload_outputs(backend_url: str, job_id: str, output_dir: str, filenames: list[str] | None = None) -> list[str]:
    if filenames is None:
        files = sorted(f for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f)))
    else:
        files = sorted(f for f in filenames if os.path.isfile(os.path.join(output_dir, f)))
    if not files:
        log.warning("No output files found after render")
        return []

    resp = requests.post(f"{backend_url}/jobs/{job_id}/request-upload-urls", json={"filenames": files}, timeout=30)
    resp.raise_for_status()
    urls: dict = resp.json()["urls"]

    uploaded: list[str] = []
    for i, fname in enumerate(files):
        url = urls.get(fname)
        if not url:
            raise RuntimeError(f"No presigned URL returned for {fname}")
        fpath = os.path.join(output_dir, fname)
        fsize = os.path.getsize(fpath)
        log.info(f"Uploading {i + 1}/{len(files)}: {fname} ({fsize / 1024 / 1024:.1f} MB)")
        with open(fpath, "rb") as fh:
            put_resp = requests.put(url, data=fh, headers={"Content-Type": "application/octet-stream"}, timeout=600)
            put_resp.raise_for_status()
        uploaded.append(fname)

    requests.post(f"{backend_url}/jobs/{job_id}/register-outputs", json={"filenames": uploaded}, timeout=30).raise_for_status()
    return uploaded


class IncrementalOutputUploader:
    def __init__(self, backend_url: str, job_id: str, output_dir: str):
        self.backend_url = backend_url.rstrip("/")
        self.job_id = job_id
        self.output_dir = output_dir
        self._uploaded: set[str] = set()
        self._last_sizes: dict[str, int] = {}
        self._stable_counts: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def uploaded(self) -> list[str]:
        return sorted(self._uploaded)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"output-uploader-{self.job_id[:8]}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan_once(require_stable=True)
            except Exception as exc:
                log.warning(f"Incremental upload scan failed for {self.job_id}: {exc}")
            self._stop.wait(OUTPUT_SCAN_INTERVAL)

    def _candidate_files(self) -> list[str]:
        try:
            return sorted(f for f in os.listdir(self.output_dir) if not f.startswith(".") and os.path.isfile(os.path.join(self.output_dir, f)))
        except FileNotFoundError:
            return []

    def _scan_once(self, require_stable: bool):
        ready: list[str] = []
        for fname in self._candidate_files():
            if fname in self._uploaded:
                continue
            fpath = os.path.join(self.output_dir, fname)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if size <= 0:
                continue
            last_size = self._last_sizes.get(fname)
            if last_size == size:
                self._stable_counts[fname] = self._stable_counts.get(fname, 0) + 1
            else:
                self._stable_counts[fname] = 0
            self._last_sizes[fname] = size
            if not require_stable or self._stable_counts.get(fname, 0) >= 1:
                ready.append(fname)
        if ready:
            self._upload_batch(ready)

    def _upload_batch(self, filenames: list[str]):
        resp = requests.post(f"{self.backend_url}/jobs/{self.job_id}/request-upload-urls", json={"filenames": filenames}, timeout=30)
        resp.raise_for_status()
        urls: dict = resp.json().get("urls", {})

        uploaded_now: list[str] = []
        for fname in filenames:
            if fname in self._uploaded:
                continue
            url = urls.get(fname)
            if not url:
                log.warning(f"No presigned URL returned for {fname}")
                continue
            fpath = os.path.join(self.output_dir, fname)
            try:
                with open(fpath, "rb") as fh:
                    put_resp = requests.put(url, data=fh, headers={"Content-Type": "application/octet-stream"}, timeout=600)
                    put_resp.raise_for_status()
            except Exception as exc:
                log.warning(f"Failed uploading frame {fname}: {exc}")
                continue
            uploaded_now.append(fname)

        if not uploaded_now:
            return

        requests.post(f"{self.backend_url}/jobs/{self.job_id}/register-outputs", json={"filenames": uploaded_now}, timeout=30).raise_for_status()
        for fname in uploaded_now:
            self._uploaded.add(fname)
            self._last_sizes.pop(fname, None)
            self._stable_counts.pop(fname, None)
        log.info(f"Registered {len(uploaded_now)} incremental output file(s) (total uploaded: {len(self._uploaded)})")

    def flush_final(self):
        self._scan_once(require_stable=False)
        self._scan_once(require_stable=False)


def _put_status(backend_url: str, job_id: str, payload: dict, deadline: float = 600) -> None:
    delay = 5
    last_exc = None
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            requests.put(f"{backend_url}/jobs/{job_id}/status", json=payload, timeout=60).raise_for_status()
            return
        except Exception as e:
            last_exc = e
            elapsed = time.monotonic() - start
            log.warning(f"Status update attempt {attempt} failed ({elapsed:.0f}s elapsed): {e}")
            if time.monotonic() - start + delay >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise last_exc


def _mark_done(backend_url: str, job_id: str, output_files: list[str]):
    _put_status(backend_url, job_id, {"status": "done", "output_files": output_files})


def _mark_failed(backend_url: str, job_id: str, error: str):
    try:
        _put_status(backend_url, job_id, {"status": "failed", "error": error})
    except Exception as e:
        log.error(f"Failed to mark job failed after retries: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    job_id = os.environ.get("JOB_ID", "").strip()
    blend_url = os.environ.get("BLEND_URL", "").strip()
    backend_url = os.environ.get("BACKEND_URL", "").strip().rstrip("/")
    render_overrides_b64 = os.environ.get("RENDER_OVERRIDES_B64", "")

    try:
        frame_start = int(os.environ["FRAME_START"])
        frame_end = int(os.environ["FRAME_END"])
        frame_step = int(os.environ.get("FRAME_STEP", "1"))
    except (KeyError, ValueError) as e:
        log.error(f"Missing or invalid frame env vars: {e}")
        return 1

    if not job_id or not blend_url or not backend_url:
        log.error(f"Missing required env vars: JOB_ID={job_id!r} BLEND_URL={blend_url!r} BACKEND_URL={backend_url!r}")
        return 1

    log.info(f"Job {job_id}: frames {frame_start}-{frame_end} step {frame_step}")

    phase_tracker = PhaseTracker()
    process_sampler = ProcessSampler()
    bytes_progress = BytesProgress()
    heartbeat = HeartbeatSender(
        backend_url=backend_url,
        job_id=job_id,
        phase_tracker=phase_tracker,
        process_sampler=process_sampler,
        bytes_progress=bytes_progress,
        interval_sec=HEARTBEAT_INTERVAL,
    )
    heartbeat.start()

    try:
        with tempfile.TemporaryDirectory() as workdir:
            input_dir = os.path.join(workdir, "input")
            output_dir = os.path.join(workdir, "output")
            os.makedirs(input_dir)
            os.makedirs(output_dir)

            # 1. Mark running
            _mark_running(backend_url, job_id)

            # 2. Download .blend (range-resume on connection drops)
            heartbeat.set_phase("download")
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
                _mark_failed(backend_url, job_id, err)
                return 1

            # bytes_progress is a download-phase signal; reset before
            # leaving the phase so subsequent rules don't see stale data.
            bytes_progress.reset()
            if filename.lower().endswith(".zip"):
                heartbeat.set_phase("extract")

            blend_files = _find_blend_files(input_dir)
            if not blend_files:
                err = "No .blend file found in uploaded input bundle"
                log.error(err)
                _mark_failed(backend_url, job_id, err)
                return 1

            blend_path, selected_from_root = _choose_render_target_blend(filename, input_dir, blend_files)
            chosen_rel = os.path.relpath(blend_path, input_dir).replace("\\", "/")
            if len(blend_files) > 1:
                candidates = sorted((os.path.relpath(p, input_dir).replace("\\", "/") for p in blend_files), key=lambda rel: (rel.count("/"), len(rel), rel.lower()))
                preview = ", ".join(candidates[:4])
                extra = "" if len(candidates) <= 4 else ", ..."
                selection_mode = "root-level priority" if selected_from_root else "fallback (no root-level .blend found)"
                log.warning(f"Found {len(blend_files)} .blend files in bundle. Selected '{chosen_rel}' ({selection_mode}). Candidates: {preview}{extra}")
            else:
                log.info(f"Selected render target: {chosen_rel}")

            # 3. Decode render overrides
            render_overrides: dict = {}
            if render_overrides_b64:
                try:
                    render_overrides = json.loads(base64.b64decode(render_overrides_b64).decode())
                except Exception as e:
                    log.warning(f"Failed to decode render_overrides_b64: {e}")

            device_policy = render_overrides.get("render", {}).get("device_policy", "AUTO").upper()

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

            heartbeat.set_phase("loading")
            log.info(f"Starting render: {RENDER_SH}")
            proc = subprocess.Popen(
                ["bash", RENDER_SH],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            uploader = IncrementalOutputUploader(backend_url, job_id, output_dir)
            uploader.start()
            egl_watchdog = EGLWatchdog(proc, EGL_WATCHDOG_SEC)
            egl_watchdog.start()
            heartbeat.set_phase("rendering")

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
                    for pattern in RENDER_WEDGE_PATTERNS:
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
                    if now - last_push >= PROGRESS_PUSH_INTERVAL:
                        _push_progress(backend_url, job_id, rendered_frames, total_frames)
                        last_push = now

            proc.wait()
            egl_watchdog.stop()
            uploader.stop()
            heartbeat.set_phase("uploading")
            try:
                uploader.flush_final()
            except Exception as exc:
                log.warning(f"Final incremental output flush failed: {exc}")
            uploaded = uploader.uploaded

            if proc.returncode != 0 or fatal_render_error or egl_watchdog.fired:
                if egl_watchdog.fired:
                    err = (
                        f"EGL watchdog killed Blender after {EGL_WATCHDOG_SEC:.0f}s "
                        f"without progress ({egl_watchdog.first_error_line})"
                    )
                elif fatal_render_error:
                    err = f"Render runtime error detected: {fatal_render_error}"
                else:
                    err = f"render.sh exited with code {proc.returncode}"
                if uploaded:
                    err = f"{err}. {len(uploaded)} frame(s) already uploaded and recoverable."
                log.error(err)
                _mark_failed(backend_url, job_id, err)
                return 1

            # 6. Catch up any files not incrementally uploaded
            local_files = sorted(f for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f)))
            missing_files = [f for f in local_files if f not in uploaded]
            if missing_files:
                log.info(f"Uploading remaining {len(missing_files)} output file(s)")
                try:
                    catch_up = _upload_outputs(backend_url, job_id, output_dir, filenames=missing_files)
                    uploaded += [f for f in catch_up if f not in uploaded]
                except Exception as e:
                    err = f"Output catch-up upload failed: {e}"
                    log.error(err)
                    _mark_failed(backend_url, job_id, err)
                    return 1

            if not uploaded:
                err = "Render produced no output files"
                log.error(err)
                _mark_failed(backend_url, job_id, err)
                return 1

            # 7. Finish.  Completion is decided by the orchestrator based on
            # registered output files — we do not self-declare "done".  The
            # backend monitor will destroy this Vast instance once it detects
            # completion.
            log.info(f"Job {job_id} finished rendering - {len(uploaded)} files uploaded")
            return 0
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    sys.exit(main())

