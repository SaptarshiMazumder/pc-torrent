"""
PC Rent - SSH Agent for RunPod Linux GPU Machines

Manages remote Linux machines (RunPod) via SSH. Detects hardware,
installs Blender, registers with backend, polls for jobs, executes
renders remotely, and streams results back — all over SSH/SFTP.

Usage:
    python ssh_agent.py --config runpod_machines.json
    python ssh_agent.py --host <host> --port <port> --user <user> --key <key_path>

Config file format (runpod_machines.json):
    [
        {
            "host": "1.2.3.4",
            "port": 22,
            "username": "root",
            "key_path": "~/.ssh/runpod_key",   // or use "password"
            "password": null,
            "label": "RunPod A6000"             // optional display name
        }
    ]
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import paramiko
import requests

# -----------------------------------------------
# CONFIG
# -----------------------------------------------
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
POLL_INTERVAL = 5          # seconds between job polls
HEARTBEAT_INTERVAL = 5.0   # seconds between heartbeats
RENDER_TIMEOUT = 4 * 3600  # 4 hours max
BACKEND_PROGRESS_MIN_INTERVAL = 1.0

BLENDER_VERSION = "5.0.1"
BLENDER_URL = f"https://download.blender.org/release/Blender5.0/blender-{BLENDER_VERSION}-linux-x64.tar.xz"
BLENDER_ARCHIVE = f"blender-{BLENDER_VERSION}-linux-x64.tar.xz"
BLENDER_DIR = f"blender-{BLENDER_VERSION}-linux-x64"

REMOTE_BASE = "/tmp/pcrent"
REMOTE_BLENDER = f"{REMOTE_BASE}/blender"
REMOTE_SCRIPTS = f"{REMOTE_BASE}/scripts"
REMOTE_JOBS = f"{REMOTE_BASE}/jobs"

PROGRESS_EVENT_PREFIX = "PCR_PROGRESS "
MISSING_LIBRARY_MARKERS = (
    "Cannot find lib",
    "linked data-blocks are missing",
    "missing from '/",
)

# Path to render scripts (same directory as this file)
AGENT_DIR = Path(__file__).parent
RENDER_SH = AGENT_DIR.parent / "server" / "docker" / "render.sh"
PROGRESS_HANDLER = AGENT_DIR.parent / "server" / "docker" / "progress_handler.py"


def _read_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


# Keep each multipart upload below typical platform request limits (e.g. Cloud Run).
OUTPUT_UPLOAD_MAX_REQUEST_MB = _read_positive_int_env("OUTPUT_UPLOAD_MAX_REQUEST_MB", 24)
OUTPUT_UPLOAD_MAX_FILES_PER_REQUEST = _read_positive_int_env("OUTPUT_UPLOAD_MAX_FILES_PER_REQUEST", 50)


# -----------------------------------------------
# LOGGING
# -----------------------------------------------
def log(label: str, message: str, level: str = "info"):
    prefix = f"[{label}]"
    print(f"{prefix} {message}", flush=True)


# -----------------------------------------------
# SSH CONNECTION
# -----------------------------------------------
def make_ssh_client(host: str, port: int, username: str,
                    key_path: str | None, password: str | None) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = dict(hostname=host, port=port, username=username, timeout=30)
    if key_path:
        connect_kwargs["key_filename"] = os.path.expanduser(key_path)
    if password:
        connect_kwargs["password"] = password

    client.connect(**connect_kwargs)
    return client


def ssh_run(client: paramiko.SSHClient, command: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a command on remote, return (exit_code, stdout, stderr)."""
    _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, out, err


def ssh_run_stream(client: paramiko.SSHClient, command: str,
                   on_line=None, timeout: int = RENDER_TIMEOUT,
                   stop_event: threading.Event = None) -> int:
    """Run a command and stream output line by line. Returns exit code."""
    transport = client.get_transport()
    chan = transport.open_session()
    chan.settimeout(timeout)
    chan.exec_command(command)

    buffer = b""
    while True:
        if stop_event and stop_event.is_set():
            chan.close()
            return -1

        if chan.recv_ready():
            chunk = chan.recv(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded and on_line:
                    on_line(decoded)
        elif chan.exit_status_ready():
            # Drain remaining output
            while chan.recv_ready():
                chunk = chan.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            if buffer:
                decoded = buffer.decode("utf-8", errors="replace").rstrip()
                if decoded and on_line:
                    on_line(decoded)
            break
        else:
            time.sleep(0.05)

    return chan.recv_exit_status()


# -----------------------------------------------
# HARDWARE DETECTION (remote)
# -----------------------------------------------
def detect_remote_specs(client: paramiko.SSHClient, machine_key: str) -> dict:
    """Detect GPU, CPU, RAM from remote Linux machine via SSH."""

    # GPU via nvidia-smi
    gpu_model = "Unknown GPU"
    gpu_vram_gb = 0.0
    nvidia_driver = None

    rc, out, _ = ssh_run(client, "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits")
    if rc == 0 and out:
        parts = [p.strip() for p in out.split(",")]
        if len(parts) >= 1:
            gpu_model = parts[0]
        if len(parts) >= 2:
            try:
                gpu_vram_gb = round(int(parts[1]) / 1024, 1)
            except ValueError:
                pass
        if len(parts) >= 3:
            nvidia_driver = parts[2]

    # CPU cores
    cpu_cores = 1
    rc, out, _ = ssh_run(client, "nproc")
    if rc == 0 and out.strip().isdigit():
        cpu_cores = int(out.strip())

    # RAM via /proc/meminfo
    ram_gb = 0.0
    rc, out, _ = ssh_run(client, "grep MemTotal /proc/meminfo")
    if rc == 0 and out:
        # "MemTotal:       32745588 kB"
        parts = out.split()
        if len(parts) >= 2:
            try:
                ram_gb = round(int(parts[1]) / (1024 * 1024), 1)
            except ValueError:
                pass

    # OS version
    rc, out, _ = ssh_run(client, "cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'")
    os_version = out.strip() if rc == 0 and out else "Linux"

    return {
        "machine_key": machine_key,
        "gpu_model": gpu_model,
        "gpu_vram_gb": gpu_vram_gb,
        "cpu_cores": cpu_cores,
        "ram_gb": ram_gb,
        "os_version": os_version,
        "nvidia_driver": nvidia_driver,
        "machine_type": "linux_ssh",
    }


def make_machine_key(host: str, username: str) -> str:
    """Stable identity key for a RunPod machine."""
    raw = f"runpod|{username}@{host}"
    return hashlib.sha256(raw.encode()).hexdigest()


# -----------------------------------------------
# BLENDER SETUP (remote)
# -----------------------------------------------
def ensure_blender(client: paramiko.SSHClient, label: str) -> bool:
    """Install Blender on the remote machine if not already present."""
    rc, _, _ = ssh_run(client, f"{REMOTE_BLENDER}/blender --version")
    if rc == 0:
        log(label, f"Blender already installed at {REMOTE_BLENDER}")
        return True

    log(label, f"Installing Blender {BLENDER_VERSION} on remote machine...")

    # Create dirs
    ssh_run(client, f"mkdir -p {REMOTE_BASE} {REMOTE_SCRIPTS} {REMOTE_JOBS}")

    # Download + extract
    download_cmd = (
        f"cd {REMOTE_BASE} && "
        f"wget -q --show-progress '{BLENDER_URL}' -O {BLENDER_ARCHIVE} && "
        f"tar xf {BLENDER_ARCHIVE} && "
        f"mv {BLENDER_DIR} blender && "
        f"rm {BLENDER_ARCHIVE}"
    )
    rc, out, err = ssh_run(client, download_cmd, timeout=600)
    if rc != 0:
        log(label, f"Failed to install Blender: {err}", level="error")
        return False

    # Verify
    rc, out, _ = ssh_run(client, f"{REMOTE_BLENDER}/blender --version")
    if rc != 0:
        log(label, "Blender installation verification failed", level="error")
        return False

    log(label, f"Blender installed: {out.splitlines()[0] if out else '?'}")
    return True


def upload_render_scripts(client: paramiko.SSHClient, label: str) -> bool:
    """Upload render.sh and progress_handler.py to the remote machine."""
    sftp = client.open_sftp()
    try:
        ssh_run(client, f"mkdir -p {REMOTE_SCRIPTS}")

        # Upload render.sh
        if RENDER_SH.exists():
            sftp.put(str(RENDER_SH), f"{REMOTE_SCRIPTS}/render.sh")
            ssh_run(client, f"chmod +x {REMOTE_SCRIPTS}/render.sh")
        else:
            log(label, f"render.sh not found at {RENDER_SH}, writing inline", level="warn")
            _write_inline_render_sh(client)

        # Upload progress_handler.py
        if PROGRESS_HANDLER.exists():
            sftp.put(str(PROGRESS_HANDLER), f"{REMOTE_SCRIPTS}/progress_handler.py")
        else:
            log(label, f"progress_handler.py not found at {PROGRESS_HANDLER}, writing inline", level="warn")
            _write_inline_progress_handler(client)

        log(label, "Render scripts uploaded")
        return True
    except Exception as e:
        log(label, f"Failed to upload render scripts: {e}", level="error")
        return False
    finally:
        sftp.close()


def _write_inline_render_sh(client: paramiko.SSHClient):
    """Fallback: write render.sh inline if the local file is missing."""
    script = r"""#!/bin/bash
set -euo pipefail
BLEND_FILE="${BLEND_FILE:-}"
if [ -z "$BLEND_FILE" ]; then
    BLEND_FILE=$(find /input -name "*.blend" -print -quit 2>/dev/null || find "$INPUT_DIR" -name "*.blend" -print -quit)
fi
BLENDER="${BLENDER_BIN:-/tmp/pcrent/blender/blender}"
OUTPUT_DIR="${OUTPUT_DIR:-/output}"
INPUT_DIR="${INPUT_DIR:-/input}"
BLEND_FILE="${BLEND_FILE:-$(find "$INPUT_DIR" -name '*.blend' | head -1)}"
if [ -z "$BLEND_FILE" ] || [ ! -f "$BLEND_FILE" ]; then
    echo "ERROR: No .blend file found"; exit 1
fi
echo "=== PC Rent Render (Linux SSH) ==="
echo "Blend file: $BLEND_FILE"
run_render() {
    local device="$1" label="$2"
    local cmd=("$BLENDER" -b "$BLEND_FILE" -P /tmp/pcrent/scripts/progress_handler.py -o "$OUTPUT_DIR/frame####" -E CYCLES)
    if [ -n "${FRAME_START:-}" ] && [ -n "${FRAME_END:-}" ]; then
        cmd+=(-s "$FRAME_START" -e "$FRAME_END")
    fi
    cmd+=(-a)
    if [ -n "$device" ]; then cmd+=(-- --cycles-device "$device"); fi
    echo "Rendering with: $label"
    "${cmd[@]}" 2>&1
}
if nvidia-smi > /dev/null 2>&1; then
    for device in OPTIX CUDA; do
        if run_render "$device" "$device (GPU)"; then exit 0; fi
    done
fi
run_render "" "CPU"
"""
    _, stdin, _ = client.exec_command(f"cat > {REMOTE_SCRIPTS}/render.sh && chmod +x {REMOTE_SCRIPTS}/render.sh")
    stdin.write(script)
    stdin.channel.shutdown_write()


def _write_inline_progress_handler(client: paramiko.SSHClient):
    """Fallback: write progress_handler.py inline if the local file is missing."""
    script = '''import json, sys
import bpy
from bpy.app.handlers import persistent
PROGRESS_PREFIX = "PCR_PROGRESS "
_state = {"total_frames": None, "rendered_frames": 0}
def emit_progress(kind, scene):
    payload = {"kind": kind, "current_frame": int(scene.frame_current),
               "rendered_frames": _state["rendered_frames"], "total_frames": _state["total_frames"]}
    sys.stdout.write(PROGRESS_PREFIX + json.dumps(payload, sort_keys=True) + "\\n")
    sys.stdout.flush()
def compute_total(scene):
    s, e, step = int(scene.frame_start), int(scene.frame_end), max(1, int(scene.frame_step))
    return ((e - s) // step) + 1 if e >= s else 0
@persistent
def on_init(scene, _=None):
    _state["total_frames"] = compute_total(scene)
    _state["rendered_frames"] = 0
    emit_progress("meta", scene)
@persistent
def on_write(scene, _=None):
    total = _state["total_frames"] or compute_total(scene)
    _state["total_frames"] = total
    _state["rendered_frames"] = min(_state["rendered_frames"] + 1, total or 999999)
    emit_progress("frame", scene)
bpy.app.handlers.render_init.append(on_init)
bpy.app.handlers.render_write.append(on_write)
'''
    _, stdin, _ = client.exec_command(f"cat > {REMOTE_SCRIPTS}/progress_handler.py")
    stdin.write(script)
    stdin.channel.shutdown_write()


# -----------------------------------------------
# BACKEND COMMUNICATION
# -----------------------------------------------
def register_machine(specs: dict) -> str:
    resp = requests.post(f"{BACKEND_URL}/machines/register", json=specs, timeout=15)
    resp.raise_for_status()
    return resp.json()["machine_id"]


def set_available(machine_id: str):
    requests.put(f"{BACKEND_URL}/machines/{machine_id}/available", timeout=10).raise_for_status()


def set_idle(machine_id: str):
    try:
        requests.put(f"{BACKEND_URL}/machines/{machine_id}/idle", timeout=10)
    except Exception:
        pass


def send_heartbeat(machine_id: str):
    requests.put(f"{BACKEND_URL}/machines/{machine_id}/heartbeat", timeout=10).raise_for_status()


def poll_for_job(machine_id: str) -> dict | None:
    resp = requests.get(f"{BACKEND_URL}/jobs/next-for-machine/{machine_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def update_job_status(job_id: str, status: str, error: str | None = None, output_files: list | None = None):
    payload = {"status": status}
    if error:
        payload["error"] = error
    if output_files is not None:
        payload["output_files"] = output_files
    requests.put(f"{BACKEND_URL}/jobs/{job_id}/status", json=payload, timeout=15).raise_for_status()


def update_job_progress(job_id: str, rendered_frames: int, total_frames: int | None):
    requests.put(
        f"{BACKEND_URL}/jobs/{job_id}/progress",
        json={"rendered_frames": rendered_frames, "total_frames": total_frames},
        timeout=10,
    ).raise_for_status()


def upload_output_files(job_id: str, local_files: list[tuple[str, bytes]]) -> list[str]:
    """Upload output frames to the backend. local_files = [(filename, data), ...]"""
    if not local_files:
        return []

    max_request_bytes = OUTPUT_UPLOAD_MAX_REQUEST_MB * 1024 * 1024
    batches: list[tuple[list[tuple[str, bytes]], int]] = []
    current_batch: list[tuple[str, bytes]] = []
    current_batch_bytes = 0

    for item in local_files:
        _, file_data = item
        file_size = len(file_data)
        exceeds_size_limit = current_batch and (current_batch_bytes + file_size > max_request_bytes)
        exceeds_file_count = current_batch and (len(current_batch) >= OUTPUT_UPLOAD_MAX_FILES_PER_REQUEST)
        if exceeds_size_limit or exceeds_file_count:
            batches.append((current_batch, current_batch_bytes))
            current_batch = []
            current_batch_bytes = 0

        current_batch.append(item)
        current_batch_bytes += file_size

    if current_batch:
        batches.append((current_batch, current_batch_bytes))

    uploaded_names: list[str] = []
    for batch, batch_bytes in batches:
        streams: list[io.BytesIO] = []
        file_tuples = []
        try:
            for filename, data in batch:
                stream = io.BytesIO(data)
                streams.append(stream)
                file_tuples.append(("files", (filename, stream)))

            timeout = max(120, min(900, 60 + int(batch_bytes / (1024 * 1024)) * 10))
            resp = requests.post(
                f"{BACKEND_URL}/jobs/{job_id}/output",
                files=file_tuples,
                timeout=timeout,
            )
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 413:
                    if len(batch) == 1:
                        filename = batch[0][0]
                        raise RuntimeError(
                            f"Output upload rejected (413): '{filename}' "
                            f"is {batch_bytes / (1024 * 1024):.1f} MB, above server request limits. "
                            "Render to a smaller file or use a compression/output format with smaller frames."
                        ) from exc
                    raise RuntimeError(
                        "Output upload rejected (413): request payload exceeded server request limits. "
                        "Set OUTPUT_UPLOAD_MAX_REQUEST_MB lower to force smaller upload batches."
                    ) from exc
                raise
        finally:
            for stream in streams:
                stream.close()

        uploaded_names.extend(filename for filename, _ in batch)

    return uploaded_names


def download_input_file(url: str) -> bytes:
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    buf = io.BytesIO()
    for chunk in resp.iter_content(chunk_size=65536):
        buf.write(chunk)
    return buf.getvalue()


# -----------------------------------------------
# PROGRESS PARSING
# -----------------------------------------------
def parse_progress_line(line: str) -> dict | None:
    if not line.startswith(PROGRESS_EVENT_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_EVENT_PREFIX):].strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") not in {"meta", "frame"}:
        return None
    try:
        total = int(payload["total_frames"]) if payload.get("total_frames") is not None else None
        rendered = max(0, int(payload.get("rendered_frames") or 0))
        current = int(payload["current_frame"]) if payload.get("current_frame") is not None else None
        if total and total > 0:
            rendered = min(rendered, total)
        return {"kind": payload["kind"], "total_frames": total, "rendered_frames": rendered, "current_frame": current}
    except (TypeError, ValueError):
        return None


# -----------------------------------------------
# JOB EXECUTION (over SSH)
# -----------------------------------------------
def execute_job(client: paramiko.SSHClient, job: dict, machine_id: str, label: str,
                stop_event: threading.Event, pause_event: threading.Event):
    job_id = job["id"]
    input_filename = job["input_filename"]
    input_url = job["input_url"]

    remote_job_dir = f"{REMOTE_JOBS}/{job_id}"
    remote_input_dir = f"{remote_job_dir}/input"
    remote_output_dir = f"{remote_job_dir}/output"

    final_status = "failed"
    final_error = None

    heartbeat_stop = threading.Event()
    progress_state = {
        "rendered_frames": 0,
        "total_frames": None,
        "last_push_at": 0.0,
        "last_reported_rendered": None,
        "last_reported_total": None,
    }

    def heartbeat_loop():
        while not heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            try:
                send_heartbeat(machine_id)
            except Exception:
                pass

    def push_progress(force=False):
        rf = progress_state["rendered_frames"]
        tf = progress_state["total_frames"]
        if tf and tf > 0:
            rf = min(rf, tf)
        if (progress_state["last_reported_rendered"] == rf and
                progress_state["last_reported_total"] == tf):
            return
        now = time.monotonic()
        if not force and (now - progress_state["last_push_at"]) < BACKEND_PROGRESS_MIN_INTERVAL:
            return
        try:
            update_job_progress(job_id, rf, tf)
            progress_state["last_push_at"] = now
            progress_state["last_reported_rendered"] = rf
            progress_state["last_reported_total"] = tf
        except Exception as e:
            log(label, f"Progress push failed: {e}", level="warn")

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        # 1. Create remote job dirs
        ssh_run(client, f"mkdir -p {remote_input_dir} {remote_output_dir}")

        # 2. Download blend file locally, SFTP it over
        log(label, f"[JOB {job_id[:8]}] Downloading {input_filename}...")
        input_data = download_input_file(input_url)

        sftp = client.open_sftp()
        try:
            remote_input_path = f"{remote_input_dir}/{input_filename}"
            with sftp.open(remote_input_path, "wb") as f:
                f.write(input_data)

            # Handle ZIP: extract on remote
            if input_filename.lower().endswith(".zip"):
                log(label, f"[JOB {job_id[:8]}] Extracting archive...")
                rc, _, err = ssh_run(client, f"cd {remote_input_dir} && unzip -o {input_filename} && rm {input_filename}")
                if rc != 0:
                    raise RuntimeError(f"Failed to extract ZIP: {err}")
        finally:
            sftp.close()

        if stop_event.is_set():
            raise RuntimeError("Stop requested")

        # 3. Find blend file on remote
        rc, blend_path, _ = ssh_run(client, f"find {remote_input_dir} -name '*.blend' | head -1")
        if rc != 0 or not blend_path.strip():
            raise RuntimeError("No .blend file found in uploaded input")
        blend_path = blend_path.strip()
        log(label, f"[JOB {job_id[:8]}] Render target: {blend_path}")

        if stop_event.is_set():
            raise RuntimeError("Stop requested")

        # 4. Build render command
        blender_bin = f"{REMOTE_BLENDER}/blender"
        render_cmd = (
            f"BLEND_FILE={blend_path} "
            f"OUTPUT_DIR={remote_output_dir} "
            f"INPUT_DIR={remote_input_dir} "
        )
        if job.get("frame_start") is not None and job.get("frame_end") is not None:
            render_cmd += f"FRAME_START={job['frame_start']} FRAME_END={job['frame_end']} "
            log(label, f"[JOB {job_id[:8]}] Distributed render: frames {job['frame_start']}-{job['frame_end']}")

        render_cmd += (
            f"{blender_bin} -b {blend_path} "
            f"-P {REMOTE_SCRIPTS}/progress_handler.py "
            f"-o {remote_output_dir}/frame#### "
            f"-E CYCLES "
        )

        if job.get("frame_start") is not None and job.get("frame_end") is not None:
            render_cmd += f"-s {job['frame_start']} -e {job['frame_end']} "

        render_cmd += "-a -- --cycles-device CUDA 2>&1 || "
        render_cmd += (
            f"{blender_bin} -b {blend_path} "
            f"-P {REMOTE_SCRIPTS}/progress_handler.py "
            f"-o {remote_output_dir}/frame#### "
            f"-E CYCLES "
        )
        if job.get("frame_start") is not None and job.get("frame_end") is not None:
            render_cmd += f"-s {job['frame_start']} -e {job['frame_end']} "
        render_cmd += "-a"

        container_log = []

        def on_line(line: str):
            event = parse_progress_line(line)
            if event:
                if event["total_frames"] is not None:
                    existing = progress_state["total_frames"]
                    progress_state["total_frames"] = (
                        event["total_frames"] if existing is None
                        else max(existing, event["total_frames"])
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
                push_progress(force=(event["kind"] == "meta"))
            else:
                container_log.append(line)
                log(label, line, source="blender" if False else "info")

        log(label, f"[JOB {job_id[:8]}] Starting Blender render...")
        exit_code = ssh_run_stream(client, render_cmd, on_line=on_line, stop_event=stop_event)

        if stop_event.is_set():
            raise RuntimeError("Stop requested")

        missing_assets = any(m in "\n".join(container_log) for m in MISSING_LIBRARY_MARKERS)

        if exit_code != 0:
            if missing_assets:
                raise RuntimeError(
                    "Project references external assets not uploaded. "
                    "Use a packed .blend or .zip bundle."
                )
            raise RuntimeError(f"Blender exited with code {exit_code}")

        # 5. Download output files via SFTP
        log(label, f"[JOB {job_id[:8]}] Collecting output files...")
        rc, file_list, _ = ssh_run(client, f"ls {remote_output_dir}")
        if rc != 0 or not file_list.strip():
            if missing_assets:
                raise RuntimeError("Project references external assets. No output produced.")
            raise RuntimeError("Render produced no output files")

        output_filenames = [f for f in file_list.strip().splitlines() if not f.startswith(".")]
        if not output_filenames:
            raise RuntimeError("Render produced no output files")

        sftp = client.open_sftp()
        output_files_data = []
        try:
            for fname in output_filenames:
                remote_path = f"{remote_output_dir}/{fname}"
                with sftp.open(remote_path, "rb") as f:
                    data = f.read()
                output_files_data.append((fname, data))
        finally:
            sftp.close()

        # 6. Force progress to complete
        if progress_state["total_frames"] and progress_state["total_frames"] > 0:
            progress_state["rendered_frames"] = progress_state["total_frames"]
        push_progress(force=True)

        # 7. Upload to backend
        log(label, f"[JOB {job_id[:8]}] Uploading {len(output_files_data)} output files...")
        uploaded = upload_output_files(job_id, output_files_data)

        if missing_assets:
            final_error = (
                "Render finished, but project referenced external assets not uploaded. "
                "Output may be incomplete."
            )
            update_job_status(job_id, "done", error=final_error, output_files=uploaded)
            log(label, f"[JOB {job_id[:8]}] Done with warnings. Files: {uploaded}")
        else:
            update_job_status(job_id, "done", output_files=uploaded)
            log(label, f"[JOB {job_id[:8]}] Done! Files: {uploaded}")

        final_status = "done"

    except Exception as e:
        log(label, f"[JOB {job_id[:8]}] Error: {e}", level="error")
        final_error = str(e)
        push_progress(force=True)
        try:
            update_job_status(job_id, "failed", error=final_error)
        except Exception:
            pass

    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)

        # Clean up remote job directory
        try:
            ssh_run(client, f"rm -rf {remote_job_dir}")
        except Exception:
            pass

    return final_status


# -----------------------------------------------
# MACHINE WORKER (one per RunPod machine)
# -----------------------------------------------
class SSHMachineWorker:
    """Manages one RunPod SSH machine: connect, setup, poll, execute jobs."""

    def __init__(self, cfg: dict):
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 22))
        self.username = cfg.get("username", "root")
        self.key_path = cfg.get("key_path")
        self.password = cfg.get("password")
        self.label = cfg.get("label") or f"{self.username}@{self.host}"

        self.machine_id: str | None = None
        self.client: paramiko.SSHClient | None = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._job_stop_event = threading.Event()

    def _connect(self) -> bool:
        try:
            log(self.label, f"Connecting to {self.username}@{self.host}:{self.port}...")
            self.client = make_ssh_client(
                self.host, self.port, self.username,
                self.key_path, self.password
            )
            log(self.label, "SSH connection established")
            return True
        except Exception as e:
            log(self.label, f"SSH connection failed: {e}", level="error")
            return False

    def _reconnect(self) -> bool:
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.client = None
        time.sleep(5)
        return self._connect()

    def _is_connected(self) -> bool:
        if not self.client:
            return False
        transport = self.client.get_transport()
        return transport is not None and transport.is_active()

    def run(self):
        """Main worker loop for this machine."""
        while not self.stop_event.is_set():
            if not self._connect():
                log(self.label, "Retrying in 30s...")
                self.stop_event.wait(30)
                continue

            try:
                self._setup_and_poll()
            except Exception as e:
                log(self.label, f"Worker error: {e}", level="error")
            finally:
                if self.machine_id:
                    set_idle(self.machine_id)
                try:
                    if self.client:
                        self.client.close()
                except Exception:
                    pass
                self.client = None

            if not self.stop_event.is_set():
                log(self.label, "Reconnecting in 15s...")
                self.stop_event.wait(15)

    def _setup_and_poll(self):
        # 1. Install Blender if needed
        if not ensure_blender(self.client, self.label):
            raise RuntimeError("Blender setup failed")

        # 2. Upload render scripts
        if not upload_render_scripts(self.client, self.label):
            raise RuntimeError("Failed to upload render scripts")

        # 3. Detect specs and register
        machine_key = make_machine_key(self.host, self.username)
        log(self.label, "Detecting hardware specs...")
        specs = detect_remote_specs(self.client, machine_key)
        log(self.label, f"  GPU: {specs['gpu_model']} ({specs['gpu_vram_gb']} GB VRAM)")
        log(self.label, f"  CPU: {specs['cpu_cores']} cores  RAM: {specs['ram_gb']} GB")
        log(self.label, f"  OS:  {specs['os_version']}")

        log(self.label, "Registering with backend...")
        self.machine_id = register_machine(specs)
        log(self.label, f"Machine ID: {self.machine_id}")

        set_available(self.machine_id)
        log(self.label, "Available. Polling for jobs...")

        # 4. Poll loop
        while not self.stop_event.is_set():
            if not self._is_connected():
                log(self.label, "SSH connection lost, reconnecting...")
                if not self._reconnect():
                    raise RuntimeError("Could not reconnect via SSH")
                set_available(self.machine_id)

            if self.pause_event.is_set():
                time.sleep(POLL_INTERVAL)
                continue

            try:
                job = poll_for_job(self.machine_id)
            except requests.exceptions.ConnectionError:
                log(self.label, f"Cannot reach backend, retrying in {POLL_INTERVAL}s...")
                time.sleep(POLL_INTERVAL)
                continue
            except Exception as e:
                log(self.label, f"Poll error: {e}", level="warn")
                time.sleep(POLL_INTERVAL)
                continue

            if job:
                log(self.label, f"Got job: {job['id']} ({job['input_filename']})")
                self._job_stop_event.clear()
                try:
                    update_job_status(job["id"], "running")
                    execute_job(
                        self.client, job, self.machine_id, self.label,
                        self._job_stop_event, self.pause_event
                    )
                except Exception as e:
                    log(self.label, f"Job execution error: {e}", level="error")
                    try:
                        update_job_status(job["id"], "failed", error=str(e))
                    except Exception:
                        pass

                if not self.stop_event.is_set() and not self.pause_event.is_set():
                    set_available(self.machine_id)
                    log(self.label, "Back to polling...")
            else:
                time.sleep(POLL_INTERVAL)

    def stop(self):
        self.stop_event.set()
        self._job_stop_event.set()


# -----------------------------------------------
# ENTRY POINT
# -----------------------------------------------
def load_config(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]


def main():
    global BACKEND_URL

    parser = argparse.ArgumentParser(description="PC Rent SSH Agent for RunPod Linux machines")
    parser.add_argument("--config", help="Path to JSON config file with machine list")
    parser.add_argument("--host", help="SSH host")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default 22)")
    parser.add_argument("--user", default="root", help="SSH username (default root)")
    parser.add_argument("--key", help="Path to SSH private key")
    parser.add_argument("--password", help="SSH password (prefer key auth)")
    parser.add_argument("--label", help="Display name for this machine")
    parser.add_argument("--backend", default=BACKEND_URL, help=f"Backend URL (default {BACKEND_URL})")
    args = parser.parse_args()

    BACKEND_URL = args.backend

    # Build machine list from CLI args or config file
    if args.config:
        machines = load_config(args.config)
    elif args.host:
        machines = [{
            "host": args.host,
            "port": args.port,
            "username": args.user,
            "key_path": args.key,
            "password": args.password,
            "label": args.label or f"{args.user}@{args.host}",
        }]
    else:
        parser.print_help()
        sys.exit(1)

    print(f"=== PC Rent SSH Agent ===")
    print(f"Backend: {BACKEND_URL}")
    print(f"Managing {len(machines)} machine(s)")

    workers = [SSHMachineWorker(cfg) for cfg in machines]
    threads = []

    for worker in workers:
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        for worker in workers:
            worker.stop()
        for t in threads:
            t.join(timeout=10)
        print("Done.")


if __name__ == "__main__":
    main()
