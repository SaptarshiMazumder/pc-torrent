"""
Modal serverless handler for PC Rent rendering.

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
import os
import subprocess
import tempfile
import threading
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BLENDER_BIN = os.getenv("BLENDER_BIN", "/opt/blender/blender")
RENDER_SH = os.getenv("RENDER_SH", "/scripts/render.sh")
PROGRESS_SCRIPT = os.getenv("PROGRESS_SCRIPT", "/scripts/progress_handler.py")
RENDER_DRIVER_SCRIPT = os.getenv("RENDER_DRIVER_SCRIPT", "/scripts/render_driver.py")

# Push a progress update to backend at most every N seconds
PROGRESS_PUSH_INTERVAL = float(os.getenv("PROGRESS_PUSH_INTERVAL", "2"))
OUTPUT_SCAN_INTERVAL = float(os.getenv("OUTPUT_SCAN_INTERVAL", "1.0"))
FORCE_CUDA_ON_A100 = os.getenv("FORCE_CUDA_ON_A100", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
RENDER_FATAL_PATTERNS = (
    "[RENDER_DRIVER] ERROR:",
    "RuntimeError: Error: Cannot render, no camera",
    "Error: Cannot render, no camera",
    # EEVEE requires an EGL display context which Modal headless containers
    # cannot provide. These errors mean the render will produce no output.
    "EGL_BAD_MATCH",
    "EGL_BAD_DISPLAY",
    "EGL_NOT_INITIALIZED",
    "Failed to create OpenGL context",
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


def _gpu_names() -> list[str]:
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


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


class ModalHeartbeat:
    """Sends periodic heartbeats to the server so the monitor can detect
    dead/cancelled containers quickly instead of waiting for the 90-min stale timeout."""

    INTERVAL = 10  # seconds between heartbeats

    def __init__(self, backend_url: str, job_id: str):
        self._backend_url = backend_url.rstrip("/")
        self._job_id = job_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, phase: str = "starting"):
        self._phase = phase
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"modal-hb-{self._job_id[:8]}"
        )
        self._thread.start()

    def set_phase(self, phase: str):
        self._phase = phase

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self.INTERVAL):
            try:
                requests.put(
                    f"{self._backend_url}/jobs/{self._job_id}/heartbeat",
                    json={"phase": self._phase},
                    timeout=10,
                )
            except Exception as exc:
                log.warning(f"Heartbeat failed for job {self._job_id}: {exc}")


def _push_progress(backend_url: str, job_id: str, rendered_frames: int, total_frames: int):
    try:
        requests.put(
            f"{backend_url}/jobs/{job_id}/progress",
            json={"rendered_frames": rendered_frames, "total_frames": total_frames},
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Failed to push progress: {e}")


def _upload_outputs(
    backend_url: str,
    job_id: str,
    output_dir: str,
    filenames: list[str] | None = None,
) -> list[str]:
    """Upload rendered output files directly to R2 via presigned URLs (bypasses Cloud Run size limits)."""
    if filenames is None:
        files = sorted(
            f for f in os.listdir(output_dir)
            if os.path.isfile(os.path.join(output_dir, f))
        )
    else:
        files = sorted(
            f for f in filenames
            if os.path.isfile(os.path.join(output_dir, f))
        )
    if not files:
        log.warning("No output files found after render")
        return []

    # Request presigned PUT URLs from server
    log.info(f"Requesting presigned upload URLs for {len(files)} file(s)")
    resp = requests.post(
        f"{backend_url}/jobs/{job_id}/request-upload-urls",
        json={"filenames": files},
        timeout=30,
    )
    resp.raise_for_status()
    urls: dict = resp.json()["urls"]

    # Upload each file directly to R2
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

    # Register uploaded filenames with the server (updates DB output_files list)
    log.info(f"Registering {len(uploaded)} output file(s) with server")
    requests.post(
        f"{backend_url}/jobs/{job_id}/register-outputs",
        json={"filenames": uploaded},
        timeout=30,
    ).raise_for_status()

    return uploaded


class IncrementalOutputUploader:
    """
    Uploads output files to R2 as soon as they appear on disk.
    Keeps already-uploaded frames safe if the render later fails.
    """

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
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"output-uploader-{self.job_id[:8]}",
        )
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
            return sorted(
                f for f in os.listdir(self.output_dir)
                if not f.startswith(".") and os.path.isfile(os.path.join(self.output_dir, f))
            )
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
        resp = requests.post(
            f"{self.backend_url}/jobs/{self.job_id}/request-upload-urls",
            json={"filenames": filenames},
            timeout=30,
        )
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
                    put_resp = requests.put(
                        url,
                        data=fh,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=600,
                    )
                    put_resp.raise_for_status()
            except Exception as exc:
                log.warning(f"Failed uploading frame {fname}: {exc}")
                continue

            uploaded_now.append(fname)

        if not uploaded_now:
            return

        requests.post(
            f"{self.backend_url}/jobs/{self.job_id}/register-outputs",
            json={"filenames": uploaded_now},
            timeout=30,
        ).raise_for_status()

        for fname in uploaded_now:
            self._uploaded.add(fname)
            self._last_sizes.pop(fname, None)
            self._stable_counts.pop(fname, None)
        log.info(
            f"Registered {len(uploaded_now)} incremental output file(s) "
            f"(total uploaded: {len(self._uploaded)})"
        )

    def flush_final(self):
        # Render process has already stopped. Upload everything remaining.
        self._scan_once(require_stable=False)
        self._scan_once(require_stable=False)


def _put_status(backend_url: str, job_id: str, payload: dict, deadline: float = 600) -> None:
    """PUT job status with retries to tolerate ngrok/Cloud Run latency spikes."""
    delay = 5
    last_exc = None
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.put(
                f"{backend_url}/jobs/{job_id}/status",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("success") is False:
                reason = str(body.get("reason") or "backend rejected status update").strip()
                raise RuntimeError(
                    f"Backend rejected status update for job {job_id}: {reason}"
                )
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

        # 1. Mark running + start heartbeat
        _mark_running(backend_url, job_id)
        heartbeat = ModalHeartbeat(backend_url, job_id)
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
            _mark_failed(backend_url, job_id, err)
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
        blend_files = _find_blend_files(input_dir)
        if not blend_files:
            err = "No .blend file found in uploaded input bundle"
            log.error(err)
            heartbeat.stop()
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
        if device_policy in ("", "AUTO") and FORCE_CUDA_ON_A100:
            gpu_names = _gpu_names()
            if any("A100" in name.upper() for name in gpu_names):
                # A100 often stalls on first OptiX frame in this pipeline.
                # Force CUDA to avoid long "meta-only" hangs.
                log.warning(
                    "A100 detected with AUTO policy; overriding DEVICE_POLICY to CUDA"
                )
                device_policy = "CUDA"

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
        uploader = IncrementalOutputUploader(backend_url, job_id, output_dir)
        uploader.start()

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
                            log.error(
                                "Fatal render pattern detected, killing process: %s", line
                            )
                            try:
                                proc.kill()
                            except Exception:
                                pass
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
        heartbeat.set_phase("uploading")
        uploader.stop()
        try:
            uploader.flush_final()
        except Exception as exc:
            log.warning(f"Final incremental output flush failed: {exc}")
        uploaded = uploader.uploaded

        if proc.returncode != 0 or fatal_render_error:
            if fatal_render_error:
                err = f"Render runtime error detected: {fatal_render_error}"
            else:
                err = f"render.sh exited with code {proc.returncode}"
            if uploaded:
                err = f"{err}. {len(uploaded)} frame(s) already uploaded and recoverable."
            log.error(err)
            heartbeat.stop()
            _mark_failed(backend_url, job_id, err)
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
                catch_up = _upload_outputs(
                    backend_url,
                    job_id,
                    output_dir,
                    filenames=missing_files,
                )
                uploaded += [f for f in catch_up if f not in uploaded]
            except Exception as e:
                err = f"Output catch-up upload failed: {e}"
                log.error(err)
                heartbeat.stop()
                _mark_failed(backend_url, job_id, err)
                return {"status": "failed", "error": err, "output_files": uploaded}
        if not uploaded:
            err = "Render produced no output files"
            log.error(err)
            heartbeat.stop()
            _mark_failed(backend_url, job_id, err)
            return {"status": "failed", "error": err}

        # Push a final progress snapshot based on the outputs that actually made
        # it to storage so the backend can reconcile the chunk before `done`.
        try:
            _push_progress(
                backend_url,
                job_id,
                max(rendered_frames, len(uploaded)),
                total_frames,
            )
        except Exception:
            pass

        # 7. Mark done
        heartbeat.stop()
        _mark_done(backend_url, job_id, uploaded)
        log.info(f"Job {job_id} done - {len(uploaded)} files uploaded")
        return {"status": "done", "output_files": uploaded}
