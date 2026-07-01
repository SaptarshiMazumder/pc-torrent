"""
PC Rent - Desktop Agent (v2 - Dockerized GPU Rendering)
Runs on provider's Windows PC. Detects specs, bootstraps Docker,
registers with backend, polls for jobs, executes them in GPU containers.

Supports two modes:
  - Console mode: `python agent.py` (prints to terminal)
  - Sidecar mode: launched by Tauri via sidecar_main.py (emits JSON events)
"""

import os
import sys
import time
import signal
import shutil
import base64
import tempfile
import subprocess
import threading
import zipfile
import json
import requests
from datetime import datetime, timezone, timedelta

_JST = timezone(timedelta(hours=9))

from config import (
    load_machine_id,
    save_machine_id,
    ensure_config_dir,
)
from system_check import check_requirements, get_windows_version, check_nvidia_gpu
from docker_setup import (
    full_bootstrap,
    check_docker_installed,
    check_docker_running,
    get_cached_gpu_verification,
)
from worker_core import (
    BytesProgress,
    HeartbeatSender,
    PhaseTracker,
    RangeResumer,
)
from worker_core.blend_file_discovery import (
    choose_render_target,
    find_blend_files,
)
from worker_core.telemetry_parser import TelemetryParser

# Phase set the agent's heartbeats use.  Smaller than the cloud-worker set
# because the actual Blender render runs inside a child Docker container,
# so the agent doesn't see "loading"/"rendering" as distinct from a
# single opaque "running" state.
_AGENT_PHASES = frozenset({"initializing", "download", "running", "uploading"})


# -----------------------------------------------
# IPC BRIDGE (optional, for sidecar mode)
# -----------------------------------------------
# When running as a Tauri sidecar, ipc.py is used for structured events.
# In console mode, we just print() as usual.
try:
    from ipc import (
        emit_job_progress as _ipc_job_progress,
        emit_log as _ipc_log,
        emit_status as _ipc_status,
    )
    _has_ipc = True
except ImportError:
    _has_ipc = False
    _ipc_job_progress = None

_sidecar_mode_active = False


def set_sidecar_mode(enabled):
    """Enable/disable sidecar IPC mode."""
    global _sidecar_mode_active
    _sidecar_mode_active = enabled


def _log(message, source="agent", level="info"):
    """Log a message. In sidecar mode, emits JSON; in console mode, prints.
    Every line is stamped with a JST HH:MM:SS.mmm prefix so cross-referencing
    against server-side / container logs is trivial."""
    ts = datetime.now(_JST).strftime("%H:%M:%S.%f")[:-3]
    stamped = f"[{ts} JST] {message}"
    if _sidecar_mode_active and _has_ipc:
        _ipc_log(stamped, source=source, level=level)
    else:
        print(stamped)


def _emit_job_progress(**payload):
    """Emit structured per-job progress to the desktop app when available."""
    if _sidecar_mode_active and _has_ipc and _ipc_job_progress:
        _ipc_job_progress(**payload)

# -----------------------------------------------
# CONFIG
# -----------------------------------------------
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
FIREBASE_TOKEN = ""  # set by sidecar on connect
POLL_INTERVAL = 5  # seconds between job polls
RENDER_TIMEOUT = 4 * 3600  # 4 hours max per render

# Community-worker Docker image is now per-env: the server hands it
# down via GET /community/worker-image so a single agent binary can
# run against any backend (dev / staging / prod / test) and always
# pull the right image for that env.  Cached after first lookup;
# restart the agent to refresh after the admin changes Firestore.
# No fallback by design -- if the endpoint is unreachable or the
# field is missing, the agent crashes loud rather than silently
# pulling a stale or wrong-env image.
_COMMUNITY_IMAGE_CACHE: str | None = None


def community_image() -> str:
    global _COMMUNITY_IMAGE_CACHE
    if _COMMUNITY_IMAGE_CACHE is None:
        import requests
        resp = requests.get(
            f"{BACKEND_URL.rstrip('/')}/community/worker-image",
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        image = payload["image"]
        if not isinstance(image, str) or not image.strip():
            raise RuntimeError(
                f"Server returned malformed community worker image: {image!r}",
            )
        _COMMUNITY_IMAGE_CACHE = image
    return _COMMUNITY_IMAGE_CACHE

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_OUTPUT_ROOT = os.path.join(AGENT_DIR, "output")
SUPPORTED_INPUT_EXTENSIONS = {".blend", ".zip"}
MISSING_LIBRARY_MARKERS = (
    "Cannot find lib",
    "linked data-blocks are missing",
    "missing from '/",
)
MISSING_ASSETS_WARNING = (
    "Render finished, but the project referenced external Blender libraries or assets that were not uploaded. "
    "Output files were produced, but materials or linked data may be incomplete."
)
PROGRESS_EVENT_PREFIX = "PCR_PROGRESS "
BACKEND_PROGRESS_MIN_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 5.0
CANCEL_CHECK_INTERVAL = 30.0
HTTP_CONNECT_TIMEOUT = 10
HTTP_READ_TIMEOUT = 30
HTTP_STATUS_READ_TIMEOUT = 120
HTTP_RETRIES = 3
HTTP_RETRY_BACKOFF_SEC = 1.5


OUTPUT_INCREMENTAL_SCAN_INTERVAL = float(os.environ.get("OUTPUT_INCREMENTAL_SCAN_INTERVAL", "1.0"))

machine_id = None
running = True
pause_event = threading.Event()
shutdown_event = threading.Event()
active_job_lock = threading.Lock()
active_job_state = {
    "id": None,
    "container_name": None,
    "process": None,
    "work_dir": None,
    "stop_requested": False,
    "stop_reason": None,
}
OPERATOR_COMMANDS = "pause | resume | stop-job | exit | status | help"


class JobStopped(RuntimeError):
    """Raised when the active render is cancelled by the operator or shutdown flow."""


# -----------------------------------------------
# HARDWARE DETECTION
# -----------------------------------------------
def get_machine_key():
    """Build a stable identity for this physical machine."""
    import hashlib
    import socket
    import uuid as uuid_mod

    parts = []

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if machine_guid:
                parts.append(str(machine_guid))
    except Exception:
        pass

    host = os.environ.get("COMPUTERNAME") or socket.gethostname()
    if host:
        parts.append(host)

    parts.append(str(uuid_mod.getnode()))

    if not parts:
        import platform
        parts.append(platform.node() or "unknown-machine")

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cpu_cores():
    return os.cpu_count() or 1


def get_ram_gb():
    try:
        result = subprocess.run(
            ["wmic", "computersystem", "get", "TotalPhysicalMemory"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip().isdigit()]
        if lines:
            return round(int(lines[0]) / (1024**3), 1)
    except Exception:
        pass
    return 0.0


def detect_specs():
    """Detect all hardware specs + OS info for registration."""
    _, _, os_display = get_windows_version()
    gpu_present, gpu_name, gpu_vram, driver_version = check_nvidia_gpu()

    return {
        "machine_key": get_machine_key(),
        "gpu_model": gpu_name or "Unknown GPU",
        "gpu_vram_gb": gpu_vram,
        "cpu_cores": get_cpu_cores(),
        "ram_gb": get_ram_gb(),
        "os_version": os_display,
        "nvidia_driver": driver_version,
    }


# -----------------------------------------------
# BACKEND COMMUNICATION
# -----------------------------------------------
def _ensure_http_success(resp, action):
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = (resp.text or "").strip()
        if len(body) > 500:
            body = body[:500] + "..."
        detail = f"{action} failed with {resp.status_code}"
        if body:
            detail = f"{detail}: {body}"
        raise RuntimeError(detail) from exc


def _request_with_retries(method, url, *, timeout, retries=HTTP_RETRIES, **kwargs):
    last_exc = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= retries:
                break
            sleep_s = min(10.0, HTTP_RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))
            _log(
                f"[AGENT] Request retry {attempt}/{retries - 1} after error: {exc}",
                level="warn",
            )
            time.sleep(sleep_s)
    raise last_exc


def register_machine(specs, commitment_seconds):
    """POST /machines/register.

    ``commitment_seconds`` is required by the server (Phase 4 of the
    time-aware allocation plan).  Server stamps
    ``commitment_end_at = now + commitment_seconds`` on the row; the
    planner reads remaining seconds off that column for its time
    filter.  Caller (the UI) chose the value via the datetime picker.
    """
    if not commitment_seconds or commitment_seconds <= 0:
        raise RuntimeError(
            "register_machine requires commitment_seconds > 0 "
            "(set via the dashboard datetime picker)"
        )
    headers = {}
    if FIREBASE_TOKEN:
        headers["Authorization"] = f"Bearer {FIREBASE_TOKEN}"
    body = dict(specs)
    body["commitment_seconds"] = float(commitment_seconds)
    resp = _request_with_retries(
        "POST",
        f"{BACKEND_URL}/machines/register",
        json=body,
        headers=headers,
        timeout=(HTTP_CONNECT_TIMEOUT, HTTP_STATUS_READ_TIMEOUT),
    )
    _ensure_http_success(resp, "Machine registration")
    payload = resp.json()
    machine = payload.get("machine_id")
    if not machine:
        raise RuntimeError("Machine registration returned no machine_id")
    return machine


def set_commitment(machine_id, commitment_seconds):
    """PUT /machines/{id}/commitment.

    Fired on graceful disconnect with ``0`` so the planner immediately
    stops considering this machine.  Best-effort: HTTP failures are
    logged but don't block shutdown (commitment will expire naturally
    once ``commitment_end_at`` elapses).
    """
    if not machine_id:
        return
    headers = {}
    if FIREBASE_TOKEN:
        headers["Authorization"] = f"Bearer {FIREBASE_TOKEN}"
    try:
        resp = _request_with_retries(
            "PUT",
            f"{BACKEND_URL}/machines/{machine_id}/commitment",
            json={"commitment_seconds": float(commitment_seconds)},
            headers=headers,
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_STATUS_READ_TIMEOUT),
        )
        _ensure_http_success(resp, "Machine commitment update")
    except Exception as exc:
        _log(f"[AGENT] set_commitment({commitment_seconds}) failed: {exc}", level="warn")


def set_available(mid):
    resp = _request_with_retries(
        "PUT",
        f"{BACKEND_URL}/machines/{mid}/available",
        timeout=(HTTP_CONNECT_TIMEOUT, HTTP_STATUS_READ_TIMEOUT),
    )
    _ensure_http_success(resp, f"Mark machine {mid} available")


def set_idle(mid):
    try:
        requests.put(f"{BACKEND_URL}/machines/{mid}/idle", timeout=15)
    except Exception:
        pass


def poll_for_job(mid):
    resp = _request_with_retries(
        "GET",
        f"{BACKEND_URL}/jobs/next-for-machine/{mid}",
        timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
    )
    _ensure_http_success(resp, f"Poll next job for machine {mid}")
    if not resp.content:
        return None
    # serverV2 wraps the job dict in {"job": <row|null>}.  Unwrap so
    # callers can do `if job: job["id"]` instead of stumbling over the
    # truthy {"job": null} envelope.
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("job") or None


def update_job_status(job_id, status, error=None, output_files=None):
    payload = {"status": status}
    if error:
        payload["error"] = error
    if output_files is not None:
        payload["output_files"] = output_files
    resp = _request_with_retries(
        "PUT",
        f"{BACKEND_URL}/jobs/{job_id}/status",
        json=payload,
        timeout=(HTTP_CONNECT_TIMEOUT, HTTP_STATUS_READ_TIMEOUT),
        retries=5,
    )
    _ensure_http_success(resp, f"Update job {job_id} status to {status}")


def notify_orchestrator_failure(job_id, error):
    """Tell the orchestrator this community job failed so the retry /
    drain / reconcile flow fires.  The agent plays the role Vast/Modal
    monitors play in-process: it observes its own render outcome and
    notifies the orchestrator over HTTP.

    Do NOT pre-flip the DB row to ``failed`` before calling this.  The
    orchestrator's ``handle_chunk_failed`` (reached via /agent-failure
    -> CallbackRouter) owns that write, and pre-flipping trips the
    router's ``is_job_terminal`` short-circuit and silently suppresses
    the retry dispatch.

    Network failures are swallowed — the heartbeat-staleness path via
    ``CommunityMonitor`` is the eventual safety net if this notify
    can't get through.
    """
    try:
        resp = _request_with_retries(
            "POST",
            f"{BACKEND_URL}/jobs/{job_id}/agent-failure",
            json={"error": error or "Agent reported failure"},
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_STATUS_READ_TIMEOUT),
            retries=2,
        )
        if resp.status_code != 200:
            _log(
                f"[JOB] Orchestrator failure notify for {job_id} returned HTTP {resp.status_code}",
                level="warn",
            )
    except Exception as exc:
        _log(f"[JOB] Orchestrator failure notify failed for {job_id}: {exc}", level="warn")


def update_job_progress(job_id, rendered_frames, total_frames=None):
    payload = {
        "rendered_frames": rendered_frames,
        "total_frames": total_frames,
    }
    resp = _request_with_retries(
        "PUT",
        f"{BACKEND_URL}/jobs/{job_id}/progress",
        json=payload,
        timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
        retries=2,
    )
    _ensure_http_success(resp, f"Update job {job_id} progress")


def update_job_telemetry(job_id, telemetry):
    """Phase-11: stash the worker's collected data-richness telemetry on
    the job row.  Fire-and-forget -- failure here has no impact on
    rendering or completion (which is decided by registered outputs).
    """
    if not telemetry:
        return
    try:
        resp = _request_with_retries(
            "PUT",
            f"{BACKEND_URL}/jobs/{job_id}/telemetry",
            json={"telemetry": telemetry},
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            retries=1,
        )
        _ensure_http_success(resp, f"Update job {job_id} telemetry")
    except Exception as exc:
        _log(f"[JOB] Telemetry push failed (non-fatal): {exc}", level="warn")


def check_job_cancel_status(job_id):
    """Returns True iff the orchestrator has marked this job cancelled or
    failed and the worker should abort.  Network errors return False —
    don't kill a running render because of a transient hiccup; the next
    poll (30s later) will catch a real cancel.
    """
    try:
        resp = _request_with_retries(
            "GET",
            f"{BACKEND_URL}/jobs/{job_id}/cancel-status",
            timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            retries=1,
        )
    except Exception:
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("cancelled"))
    except Exception:
        return False


def _read_primary_manifest(output_dir):
    """Main-render basenames the render driver wrote to the manifest
    (``.primary_outputs.json``).  Empty set on any read error -> all
    is_primary False (the backend falls back to its naming heuristic)."""
    path = os.path.join(output_dir, ".primary_outputs.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, ValueError, OSError):
        return set()


def upload_output_files(job_id, output_dir, filenames=None):
    """Upload rendered frames via the server's presigned-URL flow:
    request URLs → PUT each file directly to GCS → register (filename,
    is_primary) pairs.  Mirrors vast_worker/scripts/handler.py._upload_outputs.
    """
    if filenames is None:
        files_found = [
            f for f in os.listdir(output_dir)
            if not f.startswith(".") and os.path.isfile(os.path.join(output_dir, f))
        ]
    else:
        files_found = [
            f for f in filenames
            if not f.startswith(".") and os.path.isfile(os.path.join(output_dir, f))
        ]
    files_found = sorted(dict.fromkeys(files_found))
    if not files_found:
        return []

    resp = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/request-upload-urls",
        json={"filenames": files_found},
        timeout=30,
    )
    resp.raise_for_status()
    urls = resp.json().get("urls", {})

    uploaded = []
    for filename in files_found:
        url = urls.get(filename)
        if not url:
            raise RuntimeError(f"No presigned URL returned for {filename}")
        file_path = os.path.join(output_dir, filename)
        file_size = os.path.getsize(file_path)
        timeout = max(120, min(900, 60 + int(file_size / (1024 * 1024)) * 10))
        with open(file_path, "rb") as fh:
            put_resp = requests.put(
                url,
                data=fh,
                headers={"Content-Type": "application/octet-stream"},
                timeout=timeout,
            )
            put_resp.raise_for_status()
        uploaded.append(filename)

    primary = _read_primary_manifest(output_dir)
    requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/register-outputs",
        json={"files": [
            {"filename": f, "is_primary": f in primary} for f in uploaded
        ]},
        timeout=30,
    ).raise_for_status()
    return uploaded


class IncrementalOutputUploader:
    """
    Upload frames as they appear in output_dir so partial results survive failures.
    """

    def __init__(self, job_id, output_dir):
        self.job_id = job_id
        self.output_dir = output_dir
        self._uploaded = set()
        self._lock = threading.Lock()
        self._last_sizes = {}
        self._stable_counts = {}
        self._stop = threading.Event()
        self._thread = None

    @property
    def uploaded_files(self):
        with self._lock:
            return sorted(self._uploaded)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"agent-uploader-{self.job_id[:8]}",
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan_once(require_stable=True)
            except Exception as exc:
                _log(f"[JOB] Incremental upload scan failed: {exc}", level="warn")
            self._stop.wait(max(0.2, OUTPUT_INCREMENTAL_SCAN_INTERVAL))

    def _scan_once(self, require_stable):
        files = sorted(
            f for f in os.listdir(self.output_dir)
            if not f.startswith(".") and os.path.isfile(os.path.join(self.output_dir, f))
        )
        ready = []
        for fname in files:
            with self._lock:
                already_uploaded = fname in self._uploaded
            if already_uploaded:
                continue
            fpath = os.path.join(self.output_dir, fname)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if size <= 0:
                continue

            prev = self._last_sizes.get(fname)
            if prev == size:
                self._stable_counts[fname] = self._stable_counts.get(fname, 0) + 1
            else:
                self._stable_counts[fname] = 0
            self._last_sizes[fname] = size

            if not require_stable or self._stable_counts.get(fname, 0) >= 1:
                ready.append(fname)

        if not ready:
            return

        uploaded_now = upload_output_files(self.job_id, self.output_dir, filenames=ready)
        with self._lock:
            for fname in uploaded_now:
                self._uploaded.add(fname)
                self._last_sizes.pop(fname, None)
                self._stable_counts.pop(fname, None)
            total_uploaded = len(self._uploaded)
        if uploaded_now:
            _log(
                f"[JOB] Incremental upload: {len(uploaded_now)} new file(s), "
                f"{total_uploaded} total uploaded"
            )

    def flush_final(self):
        # Rendering has stopped, upload everything still present.
        self._scan_once(require_stable=False)
        self._scan_once(require_stable=False)


def download_input_file(input_url, dest_path, *, on_bytes=None):
    """Download the blend / asset bundle.  Uses RangeResumer so a
    mid-stream connection drop (R2 hiccup, network blip) resumes from
    the byte that was last on disk instead of restarting from zero.
    ``on_bytes(n)`` is invoked per chunk and is wired to BytesProgress
    in execute_job so heartbeats carry bytes_progressed."""
    RangeResumer().download(input_url, dest_path, on_progress=on_bytes)


def validate_input_filename(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_INPUT_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_INPUT_EXTENSIONS))
        raise RuntimeError(f"Unsupported input file '{filename}'. Expected one of: {allowed}")


def safe_extract_zip(zip_path, dest_dir):
    base_dir = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = os.path.realpath(os.path.join(dest_dir, member.filename))
            if not member_path.startswith(base_dir + os.sep) and member_path != base_dir:
                raise RuntimeError("Project archive contains invalid paths outside the extraction directory")
        archive.extractall(dest_dir)


# NOTE: ``find_blend_files`` and the picker were previously inlined here.
# They now live in ``worker_core.blend_file_discovery`` (shared by Modal
# + Vast + Community) so a change to the picker logic happens in exactly
# one place.  The old inline picker was named ``choose_render_target_blend``;
# the canonical shared version is called ``choose_render_target``.


def prepare_job_input(downloaded_file, input_dir, blend_override=None):
    """Extract the bundle (if a zip) + pick the render target.

    ``blend_override`` is the ``blend_file_relative_path`` value from
    ``render_overrides`` -- the analyze-side dropdown pick.  When
    provided, ``choose_render_target`` uses that specific file instead
    of running the heuristic.  Missing / bogus override falls through
    to today's heuristic (see worker_core.blend_file_discovery).
    """
    validate_input_filename(os.path.basename(downloaded_file))

    if downloaded_file.lower().endswith(".zip"):
        _log("[JOB] Extracting project archive...")
        safe_extract_zip(downloaded_file, input_dir)
        try:
            os.remove(downloaded_file)
        except OSError:
            pass

    blend_files = find_blend_files(input_dir)
    if not blend_files:
        raise RuntimeError(
            "No .blend file found in the uploaded input. Upload a .blend file or a .zip project bundle."
        )
    blend_file, selected_from_root = choose_render_target(
        downloaded_file, input_dir, blend_files,
        override_relative_path=blend_override,
    )
    selected_rel = os.path.relpath(blend_file, input_dir).replace("\\", "/")

    if len(blend_files) > 1:
        sorted_rels = sorted(
            (os.path.relpath(path, input_dir).replace("\\", "/") for path in blend_files),
            key=lambda item: (item.count("/"), len(item), item.lower()),
        )
        preview = ", ".join(sorted_rels[:4])
        extra = "" if len(sorted_rels) <= 4 else ", ..."
        selection_mode = (
            "user override" if blend_override and blend_override == selected_rel
            else ("root-level priority" if selected_from_root
                  else "fallback (no root-level .blend found)")
        )
        _log(
            f"[JOB] Found {len(blend_files)} .blend files in bundle. "
            f"Auto-selected '{selected_rel}' as render target ({selection_mode}). "
            f"Candidates: {preview}{extra}",
            level="warn",
        )

    _log(f"[JOB] Render target: {selected_rel}")
    return blend_file


def detect_missing_project_assets(log_lines):
    combined = "\n".join(log_lines)
    return any(marker in combined for marker in MISSING_LIBRARY_MARKERS)


def compute_progress_pct(rendered_frames, total_frames):
    if total_frames and total_frames > 0:
        pct = rendered_frames / total_frames * 100
        return round(max(0.0, min(100.0, pct)), 1)
    return None


def parse_progress_event_line(line):
    if not line.startswith(PROGRESS_EVENT_PREFIX):
        return None

    raw_payload = line[len(PROGRESS_EVENT_PREFIX):].strip()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    kind = payload.get("kind")
    if kind not in {"meta", "frame"}:
        return None

    total_frames = payload.get("total_frames")
    rendered_frames = payload.get("rendered_frames")
    current_frame = payload.get("current_frame")

    try:
        if total_frames is not None:
            total_frames = max(0, int(total_frames))
        rendered_frames = max(0, int(rendered_frames or 0))
        if current_frame is not None:
            current_frame = int(current_frame)
    except (TypeError, ValueError):
        return None

    if total_frames and total_frames > 0:
        rendered_frames = min(rendered_frames, total_frames)

    return {
        "kind": kind,
        "total_frames": total_frames,
        "rendered_frames": rendered_frames,
        "current_frame": current_frame,
    }


def parse_job_render_overrides(job):
    """Parse the job's render-override payload (JSON string from the
    server) into a dict.  Returns ``{}`` only when the field is genuinely
    empty/None (the legitimate "no overrides" case).  Raises if the field
    is present but unparseable — silently defaulting to ``{}`` on a parse
    failure would hide real corruption (the same pattern that masked the
    EEVEE-instead-of-Cycles bug for community renders)."""
    raw = job.get("render_overrides_json")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"render_overrides_json parsed to non-object: {type(parsed).__name__}"
        )
    return parsed


def persist_job_outputs(job_id, source_output_dir, status, error=None):
    os.makedirs(LOCAL_OUTPUT_ROOT, exist_ok=True)

    job_output_dir = os.path.join(LOCAL_OUTPUT_ROOT, job_id)
    os.makedirs(job_output_dir, exist_ok=True)

    copied_files = []
    if os.path.isdir(source_output_dir):
        for name in sorted(os.listdir(source_output_dir)):
            source_path = os.path.join(source_output_dir, name)
            if not os.path.isfile(source_path) or name.startswith("."):
                continue
            dest_path = os.path.join(job_output_dir, name)
            shutil.copy2(source_path, dest_path)
            copied_files.append(name)

    metadata = {
        "job_id": job_id,
        "status": status,
        "error": error,
        "saved_files": copied_files,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    metadata_path = os.path.join(job_output_dir, "metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    _log(f"[JOB] Local output snapshot: {job_output_dir}")
    return job_output_dir, copied_files


def begin_active_job(job_id, work_dir):
    with active_job_lock:
        active_job_state.update(
            {
                "id": job_id,
                "container_name": None,
                "process": None,
                "work_dir": work_dir,
                "stop_requested": False,
                "stop_reason": None,
            }
        )


def update_active_job(**kwargs):
    with active_job_lock:
        if kwargs.get("job_id") and active_job_state["id"] != kwargs["job_id"]:
            return
        for key, value in kwargs.items():
            if key != "job_id":
                active_job_state[key] = value


def clear_active_job(job_id=None):
    with active_job_lock:
        if job_id and active_job_state["id"] != job_id:
            return
        active_job_state.update(
            {
                "id": None,
                "container_name": None,
                "process": None,
                "work_dir": None,
                "stop_requested": False,
                "stop_reason": None,
            }
        )


def get_active_job_snapshot():
    with active_job_lock:
        return dict(active_job_state)


def get_active_job_stop_reason(job_id):
    with active_job_lock:
        if active_job_state["id"] != job_id or not active_job_state["stop_requested"]:
            return None
        return active_job_state["stop_reason"] or "Render stopped"


def _stop_active_job_runtime(snapshot):
    container_name = snapshot.get("container_name")
    process = snapshot.get("process")

    if container_name:
        try:
            subprocess.run(
                ["docker", "stop", "-t", "10", container_name],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=20,
            )
        except Exception:
            pass

        try:
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=10,
            )
        except Exception:
            pass

    if process and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def request_stop_current_job(reason):
    with active_job_lock:
        if not active_job_state["id"]:
            return False

        first_request = not active_job_state["stop_requested"]
        active_job_state["stop_requested"] = True
        active_job_state["stop_reason"] = reason
        snapshot = dict(active_job_state)

    if first_request:
        _log(f"[AGENT] Stopping active render job: {snapshot['id']}")

    _stop_active_job_runtime(snapshot)
    return True


def should_offer_capacity():
    return running and not pause_event.is_set() and not shutdown_event.is_set()


def pause_agent(reason="Paused by operator"):
    if pause_event.is_set():
        _log("[AGENT] Agent is already paused.")
        return

    pause_event.set()
    _log(f"[AGENT] {reason}. The machine will stop taking new jobs.")

    if not request_stop_current_job("Render stopped because the machine was paused") and machine_id:
        set_idle(machine_id)


def resume_agent():
    if shutdown_event.is_set():
        _log("[AGENT] Cannot resume because shutdown is already in progress.")
        return

    if not pause_event.is_set():
        _log("[AGENT] Agent is already available.")
        return

    pause_event.clear()
    if machine_id:
        set_available(machine_id)
    _log("[AGENT] Agent resumed and is available for jobs.")


def shutdown_agent(reason="Shutdown requested"):
    global running

    if shutdown_event.is_set():
        return

    _log(f"[AGENT] {reason}")
    running = False
    shutdown_event.set()
    pause_event.set()

    if not request_stop_current_job("Render stopped because the agent is shutting down") and machine_id:
        set_idle(machine_id)


def print_agent_status():
    snapshot = get_active_job_snapshot()
    if snapshot["id"]:
        print(
            f"[AGENT] Status: rendering job {snapshot['id']}"
            + (" (stop requested)" if snapshot["stop_requested"] else "")
        )
    elif pause_event.is_set():
        print("[AGENT] Status: paused")
    elif shutdown_event.is_set():
        print("[AGENT] Status: shutting down")
    else:
        print("[AGENT] Status: available / polling")


def operator_command_loop():
    print(f"[AGENT] Operator commands: {OPERATOR_COMMANDS}")

    while running and not shutdown_event.is_set():
        try:
            command = input().strip().lower()
        except EOFError:
            return
        except Exception:
            return

        if not command:
            continue

        if command in {"help", "?"}:
            print(f"[AGENT] Commands: {OPERATOR_COMMANDS}")
        elif command == "pause":
            pause_agent()
        elif command == "resume":
            resume_agent()
        elif command in {"stop", "stop-job", "cancel"}:
            if not request_stop_current_job("Render stopped by operator"):
                print("[AGENT] No render job is currently running.")
        elif command in {"exit", "quit", "stop-agent"}:
            shutdown_agent("Exit requested by operator")
            return
        elif command == "status":
            print_agent_status()
        else:
            print(f"[AGENT] Unknown command: {command}")
            print(f"[AGENT] Commands: {OPERATOR_COMMANDS}")


# -----------------------------------------------
# DOCKER IMAGE MANAGEMENT
# -----------------------------------------------
def check_image_loaded():
    """Returns True iff the community render image is present in the
    local Docker daemon.  Uses the full image reference (registry + tag)
    so it doesn't false-positive on stale legacy images with the same
    short name."""
    try:
        result = subprocess.run(
            ["docker", "images", "-q", community_image()],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def get_runtime_status():
    """Collect local runtime status for the desktop app."""
    requirements = check_requirements()
    docker_installed = check_docker_installed()
    docker_running = check_docker_running() if docker_installed else False
    cached_gpu = get_cached_gpu_verification() if docker_running else None
    if not docker_installed:
        image_present = False
        image_stage = "missing"
        image_status = "Docker is not installed."
    elif not docker_running:
        image_present = None
        image_stage = "idle"
        image_status = "Start Docker to inspect the render image."
    else:
        image_present = check_image_loaded()
        image_stage = "ready" if image_present else "missing"
        image_status = "Render image is installed." if image_present else "Render image not installed."

    return {
        "requirements_checked": True,
        "requirements_ready": requirements["ready"],
        "requirement_issues": requirements["issues"],
        "docker_installed": docker_installed,
        "docker_running": docker_running,
        "gpu_verified": cached_gpu["gpu_verified"] if cached_gpu else None,
        "gpu_error": cached_gpu.get("gpu_error") if cached_gpu else None,
        "image_present": image_present,
        "image_stage": image_stage,
        "image_downloaded_bytes": None,
        "image_total_bytes": None,
        "image_progress_pct": None,
        "image_status": image_status,
    }


def remove_docker_image():
    """Delete the locally cached render image and related metadata."""
    snapshot = get_active_job_snapshot()
    if snapshot["id"]:
        return {
            "ok": False,
            "message": "Cannot remove the render image while a job is running.",
            "image_present": True,
        }

    docker_installed = check_docker_installed()
    docker_running = check_docker_running() if docker_installed else False
    if docker_installed and not docker_running:
        return {
            "ok": False,
            "message": "Start Docker Desktop before deleting the render image.",
            "image_present": None,
        }

    image_present = check_image_loaded() if docker_running else False

    if image_present:
        try:
            subprocess.run(
                ["docker", "image", "rm", "-f", community_image()],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=60,
            )
        except Exception as exc:
            return {
                "ok": False,
                "message": f"Failed to remove the render image: {exc}",
                "image_present": True,
            }

    still_present = check_image_loaded() if docker_running else False
    return {
        "ok": not still_present,
        "message": "Render image removed." if image_present else "Render image was already absent.",
        "image_present": still_present,
    }


def ensure_docker_image(on_stage=None, on_progress=None):
    """Pulls the community render image from GHCR if it's not already
    present.  Lets Docker handle staleness and layer caching natively —
    no SHA cache file, no full-tarball downloads.  ``docker pull`` is a
    no-op when the local image already matches the remote digest, so
    calling this on every job is cheap.
    """
    def stage(stage_name, message, **extra):
        if on_stage:
            on_stage(stage_name, message, **extra)

    stage("checking", "Checking render image...")
    _log(f"[IMAGE] Pulling {community_image()} ...")
    stage("downloading", "Pulling render image from registry...", progress=0)

    try:
        proc = subprocess.Popen(
            ["docker", "pull", community_image()],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8", errors="replace",
        )
        # Stream docker pull's per-layer status so the user sees progress.
        # docker pull's own output is the source of truth; we don't try
        # to translate "Pulling fs layer / Downloading / Extracting" lines
        # into a single percentage — too lossy.
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _log(f"[IMAGE] {line}")
        proc.wait(timeout=1800)  # 30 min for first pull on slow links
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
        stage("error", "docker pull timed out.")
        _log("[IMAGE] docker pull timed out after 30 minutes.")
        return False
    except Exception as e:
        stage("error", f"Failed to pull render image: {e}")
        _log(f"[IMAGE] docker pull failed: {e}")
        return False

    if proc.returncode != 0:
        stage("error", "Failed to pull render image.")
        _log(f"[IMAGE] docker pull exited with code {proc.returncode}")
        return False

    stage("ready", "Render image is ready.")
    _log("[IMAGE] Image pull complete.")
    return True


# -----------------------------------------------
# JOB EXECUTION (Docker + GPU)
# -----------------------------------------------
def execute_job(job):
    """Execute a render job inside a Docker container with GPU access."""
    # Pre-try defaults — anything the ``finally`` block touches must
    # exist whether or not the body got past field extraction.  Without
    # this, a malformed job dict (missing ``id`` / ``input_filename`` /
    # ``group_id``) would raise out of execute_job entirely and the
    # caller's poll loop would silently swallow the exception, leaving
    # the row stuck at ``status='running'`` until heartbeat staleness
    # eventually kicked in.
    job_id = "<unknown>"
    work_dir = None
    output_dir = None
    cancel_check_stop = threading.Event()
    cancel_check_thread = None
    output_uploader = None
    setup_complete = False
    final_status = "failed"
    final_error = None
    container_name = None
    process = None

    try:
        job_id = job["id"]
        input_filename = job["input_filename"]
        # serverV2's next-for-machine response doesn't include a presigned
        # input_url (Vast/Modal get theirs via BlendUrlResolver server-side).
        # Agents can hit the public download endpoint instead — it 302s to
        # a fresh presigned R2 URL each call.
        input_url = job.get("input_url") or (
            f"{BACKEND_URL}/render-groups/{job['group_id']}/input/{input_filename}"
        )
        active_machine_id = machine_id

        work_dir = os.path.join(tempfile.gettempdir(), f"pcrent_{job_id}")
        input_dir = os.path.join(work_dir, "input")
        output_dir = os.path.join(work_dir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        begin_active_job(job_id, work_dir)
        output_uploader = IncrementalOutputUploader(job_id, output_dir)
        progress_state = {
            "current_frame": None,
            "rendered_frames": 0,
            "total_frames": None,
            "last_backend_push_at": 0.0,
            "last_reported_rendered_frames": None,
            "last_reported_total_frames": None,
        }

        def start_cancel_check_loop():
            """Polls the server every CANCEL_CHECK_INTERVAL to detect a server-
            side cancel while Blender is mid-render.  On detected cancel, asks
            the local stop machinery to kill the container — same path the
            sidecar UI's Stop button takes.  Network errors are ignored; the
            next tick retries.
            """

            def cancel_check_loop():
                while not cancel_check_stop.wait(CANCEL_CHECK_INTERVAL):
                    if check_job_cancel_status(job_id):
                        _log(
                            f"[JOB] Server reported cancel for job {job_id} — stopping render",
                        )
                        request_stop_current_job("Cancelled by server")
                        return

            thread = threading.Thread(target=cancel_check_loop, daemon=True)
            thread.start()
            return thread

        def emit_progress_update():
            total_frames = progress_state["total_frames"]
            rendered_frames = progress_state["rendered_frames"]
            if total_frames and total_frames > 0:
                rendered_frames = min(rendered_frames, total_frames)
            _emit_job_progress(
                job_id=job_id,
                filename=input_filename,
                current_frame=progress_state["current_frame"],
                rendered_frames=rendered_frames,
                total_frames=total_frames,
                progress_pct=compute_progress_pct(rendered_frames, total_frames),
            )

        def push_progress_to_backend(force=False):
            total_frames = progress_state["total_frames"]
            rendered_frames = progress_state["rendered_frames"]
            if total_frames and total_frames > 0:
                rendered_frames = min(rendered_frames, total_frames)

            if (
                progress_state["last_reported_rendered_frames"] == rendered_frames
                and progress_state["last_reported_total_frames"] == total_frames
            ):
                return

            now = time.monotonic()
            if not force and (now - progress_state["last_backend_push_at"]) < BACKEND_PROGRESS_MIN_INTERVAL:
                return

            try:
                update_job_progress(
                    job_id,
                    rendered_frames=rendered_frames,
                    total_frames=total_frames,
                )
            except Exception as exc:
                progress_state["last_backend_push_at"] = now
                _log(f"[JOB] Failed to report progress: {exc}", level="warn")
                return

            progress_state["last_backend_push_at"] = now
            progress_state["last_reported_rendered_frames"] = rendered_frames
            progress_state["last_reported_total_frames"] = total_frames

        def apply_progress_event(event):
            total_frames = event.get("total_frames")
            if total_frames is not None:
                existing_total = progress_state["total_frames"]
                progress_state["total_frames"] = (
                    total_frames
                    if existing_total is None
                    else max(existing_total, total_frames)
                )

            progress_state["rendered_frames"] = max(
                progress_state["rendered_frames"],
                event.get("rendered_frames") or 0,
            )

            if progress_state["total_frames"] and progress_state["total_frames"] > 0:
                progress_state["rendered_frames"] = min(
                    progress_state["rendered_frames"],
                    progress_state["total_frames"],
                )

            if event.get("current_frame") is not None:
                progress_state["current_frame"] = event["current_frame"]

            emit_progress_update()
            push_progress_to_backend(force=event["kind"] == "meta")

        def finalize_progress(force_complete=False):
            total_frames = progress_state["total_frames"]
            if force_complete and total_frames and total_frames > 0:
                progress_state["rendered_frames"] = total_frames
            emit_progress_update()
            push_progress_to_backend(force=True)

        def with_partial_recovery_hint(message):
            uploaded_count = len(output_uploader.uploaded_files)
            if uploaded_count <= 0:
                return message
            return f"{message}. {uploaded_count} frame(s) already uploaded and recoverable."

        # All helpers defined and all setup state populated — anything from
        # this point on can rely on output_uploader / progress_state /
        # finalize_progress / with_partial_recovery_hint being callable.
        setup_complete = True

        cancel_check_thread = start_cancel_check_loop()

        # Per-job heartbeat: phase + bytes_progressed signals for the
        # server's stall detector.  Server-side, this writes Redis
        # job:{id}:hb (TTL key + sliding window) -- and the same key
        # is what CommunityMonitor reads to detect a mid-render
        # machine-offline event.  No separate machine-level heartbeat;
        # the per-job one carries that signal too.
        phase_tracker = PhaseTracker(
            allowed=_AGENT_PHASES, initial="initializing",
        )
        bytes_progress = BytesProgress()
        job_heartbeat = HeartbeatSender(
            backend_url=BACKEND_URL,
            job_id=job_id,
            phase_tracker=phase_tracker,
            bytes_progress=bytes_progress,
            interval_sec=HEARTBEAT_INTERVAL,
            on_error=lambda exc: _log(
                f"[JOB] Job heartbeat push failed: {exc}", level="warn",
            ),
        )
        job_heartbeat.start()

        # 1. Download blend file (range-resume on connection drops)
        blend_file = os.path.join(input_dir, input_filename)
        _log(f"[JOB] Downloading: {input_filename}")
        job_heartbeat.set_phase("download")
        download_input_file(input_url, blend_file, on_bytes=bytes_progress.add)
        # Parse render_overrides early so ``prepare_job_input`` can
        # honour the analyze-side ``blend_file_relative_path`` pick.
        # Empty / non-dict overrides fall through to today's heuristic.
        _render_overrides_early = parse_job_render_overrides(job)
        _blend_override = (
            _render_overrides_early.get("blend_file_relative_path")
            if isinstance(_render_overrides_early, dict)
            else None
        )
        blend_file = prepare_job_input(
            blend_file, input_dir, blend_override=_blend_override,
        )
        # Download phase done -- reset the counter so the "running"
        # phase doesn't carry stale bytes_progressed.
        bytes_progress.reset()
        job_heartbeat.set_phase("running")

        stop_reason = get_active_job_stop_reason(job_id)
        if stop_reason:
            raise JobStopped(stop_reason)

        # 2. Run Docker container with GPU
        # Convert Windows paths to forward-slash format for Docker
        input_mount = input_dir.replace("\\", "/")
        output_mount = output_dir.replace("\\", "/")
        container_name = f"pcrent-job-{job_id[:8]}"
        blend_rel = os.path.relpath(blend_file, input_dir).replace("\\", "/")
        update_active_job(job_id=job_id, container_name=container_name)

        stop_reason = get_active_job_stop_reason(job_id)
        if stop_reason:
            raise JobStopped(stop_reason)

        cmd = [
            "docker", "run", "--rm",
            "--gpus", "all",
            "--network", "none",
            "--name", container_name,
            "-e", f"BLEND_FILE=/input/{blend_rel}",
            "-v", f"{input_mount}:/input:ro",
            "-v", f"{output_mount}:/output",
        ]

        # Add frame range for distributed rendering
        if job.get("frame_start") is not None and job.get("frame_end") is not None:
            cmd.extend(["-e", f"FRAME_START={job['frame_start']}"])
            cmd.extend(["-e", f"FRAME_END={job['frame_end']}"])
            _log(
                f"[JOB] Distributed render: frames {job['frame_start']}-{job['frame_end']}"
            )

        frame_step = job.get("frame_step") or 1
        try:
            frame_step = max(1, int(frame_step))
        except (TypeError, ValueError):
            frame_step = 1
        cmd.extend(["-e", f"FRAME_STEP={frame_step}"])

        render_overrides = parse_job_render_overrides(job)
        if render_overrides:
            overrides_json = json.dumps(render_overrides, separators=(",", ":"), ensure_ascii=True)
            overrides_b64 = base64.b64encode(overrides_json.encode("utf-8")).decode("ascii")
            cmd.extend(["-e", f"RENDER_OVERRIDES_B64={overrides_b64}"])
            device_policy = (
                render_overrides.get("render", {}).get("device_policy")
                if isinstance(render_overrides.get("render"), dict)
                else None
            )
            if isinstance(device_policy, str) and device_policy.strip():
                cmd.extend(["-e", f"DEVICE_POLICY={device_policy.strip().upper()}"])
            # Output format is applied via Blender's -F flag in render.sh
            # (headless Python can't set render-only formats like
            # OPEN_EXR_MULTILAYER).  Omitted -> render.sh defaults to PNG.
            output_cfg = render_overrides.get("output")
            render_format = output_cfg.get("file_format") if isinstance(output_cfg, dict) else None
            if isinstance(render_format, str) and render_format.strip():
                cmd.extend(["-e", f"RENDER_FORMAT={render_format.strip()}"])

        cmd.append(community_image())

        _log(f"[JOB] Starting Docker render container ({community_image()})...")
        _log(f"[JOB] Command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8", errors="replace",
        )
        update_active_job(job_id=job_id, process=process)
        output_uploader.start()

        # Stream container output
        container_log = []
        render_device = ""
        # Phase-11 telemetry parser -- consumes every stdout line (cheap
        # no-op on non-matching lines), accumulates the merged data-
        # richness payload pushed to backend after render success.
        # Pure observability; failure cannot affect render or completion.
        telemetry_parser = TelemetryParser()
        try:
            for line in iter(process.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    telemetry_parser.consume(line)
                    progress_event = parse_progress_event_line(line)
                    if progress_event:
                        apply_progress_event(progress_event)
                        continue
                    container_log.append(line)
                    lower_line = line.lower()
                    if line.startswith("Rendering with:"):
                        render_device = line.split(":", 1)[1].strip()
                    elif "falling back to cpu rendering" in lower_line:
                        render_device = "CPU"
                    _log(line, source="container")
        finally:
            if process.stdout:
                process.stdout.close()

        process.wait(timeout=RENDER_TIMEOUT)
        output_uploader.stop()
        try:
            output_uploader.flush_final()
        except Exception as exc:
            _log(f"[JOB] Final incremental output flush failed: {exc}", level="warn")

        missing_assets = detect_missing_project_assets(container_log)
        stop_reason = get_active_job_stop_reason(job_id)

        if stop_reason:
            raise JobStopped(stop_reason)

        if process.returncode != 0:
            if missing_assets:
                raise RuntimeError(
                    "The project references external Blender libraries or assets that were not uploaded. "
                    "Upload a packed .blend file or a .zip bundle containing the full project folder."
                )
            raise RuntimeError(f"Container exited with code {process.returncode}")

        finalize_progress(force_complete=True)
        if render_device:
            _log(f"[JOB] Render device used: {render_device}")
        else:
            _log("[JOB] Render device used: unknown (no device marker in container logs).", level="warn")

        # Phase-11: push collected telemetry to backend.  Fire-and-forget
        # -- update_job_telemetry swallows network errors so this cannot
        # affect job completion (which is decided by registered outputs).
        update_job_telemetry(job_id, telemetry_parser.payload())

        # 4. Catch up any files that were not incrementally uploaded
        output_files = output_uploader.uploaded_files
        local_output_files = sorted(
            f for f in os.listdir(output_dir)
            if not f.startswith(".") and os.path.isfile(os.path.join(output_dir, f))
        )
        missing_output_files = [f for f in local_output_files if f not in output_files]
        if missing_output_files:
            _log(f"[JOB] Uploading remaining {len(missing_output_files)} output file(s)...")
            output_files += [
                f for f in upload_output_files(job_id, output_dir, filenames=missing_output_files)
                if f not in output_files
            ]

        if not output_files:
            if missing_assets:
                raise RuntimeError(
                    "The project references external Blender libraries or assets that were not uploaded. "
                    "No output files were produced."
                )
            raise RuntimeError("Render produced no output files")

        # Two paths now mark the job done server-side:
        #   1. /register-outputs counting in JobService -- when the
        #      uploaded count reaches total_frames, schedules
        #      notify_completion as a FastAPI BackgroundTask.
        #   2. The explicit ``status="done"`` PUT we issue in the
        #      finally block below -- runs synchronously inside the
        #      HTTP request, marks the row 'done' before the agent's
        #      next poll.
        #
        # The second path closes the reclaim race
        # (handle_community_machine_idle).  Without it, an agent
        # restart between the last register-outputs call and the
        # BackgroundTask firing leaves the row at status='running' --
        # next_for_machine's self-heal then marks the chunk failed
        # ("Agent went idle while job was running -- previous session
        # lost") even though all frames uploaded successfully.
        if missing_assets:
            final_error = MISSING_ASSETS_WARNING
            _log(f"[JOB] Done with warnings! Files: {output_files}")
            _log(f"[JOB] Warning: {final_error}")
        else:
            _log(f"[JOB] Done! Files: {output_files}")
        final_status = "done"

    except JobStopped as e:
        _log(f"[JOB] {e}")
        final_error = with_partial_recovery_hint(str(e))
        finalize_progress()
        # Don't pre-flip the DB row to 'failed' here -- the orchestrator's
        # handle_chunk_failed (reached via /agent-failure -> CallbackRouter)
        # owns that write, and pre-flipping would trip the router's
        # is_job_terminal short-circuit and silently suppress the retry
        # dispatch.  Same reasoning for the Timeout/Exception paths below.
        notify_orchestrator_failure(job_id, final_error)

    except subprocess.TimeoutExpired:
        _log(f"[JOB] Render timed out after {RENDER_TIMEOUT}s, killing container...")
        try:
            subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10)
        except Exception:
            pass
        final_error = with_partial_recovery_hint("Render timed out")
        finalize_progress()
        notify_orchestrator_failure(job_id, final_error)

    except Exception as e:
        _log(f"[JOB] Error: {e}", level="error")
        # Helpers (with_partial_recovery_hint, finalize_progress) only
        # exist if setup got past their def lines.  For early failures
        # (malformed job dict, mkdir refused, etc.), fall back to the
        # raw error message and skip the progress flush.
        if setup_complete:
            try:
                final_error = with_partial_recovery_hint(str(e))
                finalize_progress()
            except Exception as helper_exc:
                _log(f"[JOB] Cleanup helpers failed: {helper_exc}", level="warn")
                final_error = str(e)
        else:
            final_error = str(e)
        notify_orchestrator_failure(job_id, final_error)

    finally:
        try:
            job_heartbeat.stop()
        except NameError:
            # job_heartbeat is created inside the try block; if execute_job
            # raised before that point (malformed job dict, etc.) the name
            # won't exist.  Nothing to stop.
            pass
        cancel_check_stop.set()
        if cancel_check_thread and cancel_check_thread.is_alive():
            cancel_check_thread.join(timeout=1)
        if output_uploader is not None:
            try:
                output_uploader.stop()
                output_uploader.flush_final()
            except Exception as exc:
                _log(f"[JOB] Failed to finalize incremental output uploader: {exc}", level="warn")

        # No agent-side self-mark-done.  The CommunityMonitor singleton
        # detects chunk completion on its 10s tick via
        # ``JobCounts.is_complete`` (same path Vast/Modal singletons
        # use) and fires the success chain through the standard
        # callback router.  Symmetric across all three fleets.
        if output_dir is not None:
            persist_job_outputs(job_id, output_dir, final_status, final_error)
        clear_active_job(job_id)
        if pause_event.is_set() or shutdown_event.is_set():
            if machine_id:
                set_idle(machine_id)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


# -----------------------------------------------
# MAIN LOOP
# -----------------------------------------------
def shutdown_handler(sig, frame):
    threading.Thread(
        target=shutdown_agent,
        args=(f"Received signal {sig}. Shutting down...",),
        daemon=True,
    ).start()


def main():
    global machine_id, running

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    ensure_config_dir()

    _log("=== PC Rent Agent v2 (Docker GPU) ===")
    _log(f"Backend: {BACKEND_URL}")

    # Step 1: System requirements check
    _log("[AGENT] Checking system requirements...")
    req = check_requirements()
    _log(f"  OS:     {req['os_version']}")
    if req["gpu_name"]:
        _log(f"  GPU:    {req['gpu_name']} ({req['gpu_vram_gb']} GB VRAM)")
        _log(f"  Driver: {req['nvidia_driver']}")

    if not req["ready"]:
        _log("[AGENT] System requirements not met:")
        for issue in req["issues"]:
            for line in issue.split("\n"):
                _log(f"  {line}")
        _log("[AGENT] Please fix the above issues and restart the agent.")
        sys.exit(1)

    # Step 2: Docker bootstrap
    _log("[AGENT] Setting up Docker...")
    docker_result = full_bootstrap()

    if docker_result.get("needs_reboot"):
        _log(f"[AGENT] {docker_result['message']}")
        sys.exit(0)

    if not docker_result.get("ready"):
        _log(f"[AGENT] Docker setup failed: {docker_result['message']}")
        sys.exit(1)

    gpu_docker = docker_result.get("gpu_docker_name", "")
    if gpu_docker:
        _log(f"[AGENT] GPU in Docker: {gpu_docker} — ready for rendering.")
    else:
        gpu_error = docker_result.get("gpu_error", "GPU verification failed.")
        _log(f"[AGENT] WARNING: GPU not accessible in Docker. {gpu_error}")

    # Step 3: Refresh the render image so peers with a cached copy pick up
    # any new digest pushed to GHCR.  docker pull is a no-op on digest
    # match.  On failure (offline etc) any cached copy stays usable; the
    # per-job fallback in the polling loop handles the genuinely-missing
    # case.
    _log("[AGENT] Refreshing render image...")
    ensure_docker_image()
    if check_image_loaded():
        _log("[AGENT] Render image ready.")
    else:
        _log("[AGENT] No render image available — will retry on first job.")

    # Step 4: Detect hardware specs
    _log("[AGENT] Detecting hardware specs...")
    specs = detect_specs()
    _log(f"  GPU:    {specs['gpu_model']} ({specs['gpu_vram_gb']} GB VRAM)")
    _log(f"  CPU:    {specs['cpu_cores']} cores")
    _log(f"  RAM:    {specs['ram_gb']} GB")
    _log(f"  OS:     {specs['os_version']}")
    _log(f"  Driver: {specs['nvidia_driver']}")

    # Step 5: Register with backend (or reconnect with saved ID)
    saved_id = load_machine_id()
    restored_with_saved_id = False

    _log("[AGENT] Registering with backend...")
    try:
        machine_id = register_machine(specs)
        save_machine_id(machine_id)
        if saved_id and saved_id == machine_id:
            _log(f"[AGENT] Reconnected. Machine ID: {machine_id}")
        else:
            _log(f"[AGENT] Registered. Machine ID: {machine_id}")
    except Exception as e:
        _log(f"[AGENT] Failed to register: {e}", level="error")
        if saved_id:
            _log(f"[AGENT] Trying saved machine ID: {saved_id}")
            machine_id = saved_id
            try:
                set_available(machine_id)
                restored_with_saved_id = True
                _log(f"[AGENT] Recovered using saved machine ID: {machine_id}")
            except Exception as saved_err:
                _log(f"[AGENT] Saved machine ID is not usable: {saved_err}", level="error")
                sys.exit(1)
        else:
            sys.exit(1)

    # Step 6: Mark available and start polling
    if not restored_with_saved_id:
        try:
            set_available(machine_id)
        except Exception as e:
            _log(f"[AGENT] Failed to mark machine available: {e}", level="error")
            sys.exit(1)
    _log("[AGENT] Marked as available. Polling for jobs...")

    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=operator_command_loop, daemon=True).start()

    try:
        while running:
            try:
                if pause_event.is_set():
                    time.sleep(POLL_INTERVAL)
                    continue

                # Make sure Docker is still running
                if not check_docker_running():
                    _log("[AGENT] Docker is not running. Waiting...")
                    time.sleep(POLL_INTERVAL * 2)
                    continue

                job = poll_for_job(machine_id)
                if job:
                    _log(f"[AGENT] Got job: {job['id']} ({job['input_filename']})")
                    update_job_status(job["id"], "running")

                    if not check_image_loaded():
                        _log(f"[AGENT] Render image {community_image()} not loaded, downloading...")
                        if not ensure_docker_image():
                            _log("[AGENT] Cannot load render image, failing job.")
                            notify_orchestrator_failure(job["id"], "Render image not available")
                            continue

                    execute_job(job)

                    # Re-mark available after job only if still accepting work
                    if should_offer_capacity():
                        set_available(machine_id)
                        _log("[AGENT] Back to polling...")
                    elif pause_event.is_set() and not shutdown_event.is_set():
                        _log("[AGENT] Agent is paused.")
                else:
                    time.sleep(POLL_INTERVAL)

            except requests.exceptions.ConnectionError:
                _log(f"[AGENT] Cannot reach backend, retrying in {POLL_INTERVAL}s...")
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                _log(f"[AGENT] Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)
    finally:
        if machine_id:
            set_idle(machine_id)
        _log("[AGENT] Shutdown complete.")


if __name__ == "__main__":
    main()
