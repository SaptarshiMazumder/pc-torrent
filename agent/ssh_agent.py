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
import base64
import hashlib
import io
import json
import os
import shlex
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

# Path to Linux render scripts (separate from Windows image sources)
AGENT_DIR = Path(__file__).parent
RENDER_SH = AGENT_DIR.parent / "server" / "docker_linux" / "render.sh"
PROGRESS_HANDLER = AGENT_DIR.parent / "server" / "docker_linux" / "progress_handler.py"
RENDER_DRIVER = AGENT_DIR.parent / "server" / "docker_linux" / "render_driver.py"


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
    """Run a command on remote, return (exit_code, stdout, stderr).

    Reads stdout/stderr concurrently to avoid deadlocks when one stream fills.
    """
    _, stdout, _ = client.exec_command(command, timeout=timeout, get_pty=False)
    chan = stdout.channel

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    started = time.monotonic()

    while True:
        if chan.recv_ready():
            out_chunks.append(chan.recv(4096))
        if chan.recv_stderr_ready():
            err_chunks.append(chan.recv_stderr(4096))

        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break

        if timeout and (time.monotonic() - started) > timeout:
            try:
                chan.close()
            except Exception:
                pass
            raise RuntimeError(f"Remote command timed out after {timeout}s: {command}")

        time.sleep(0.05)

    exit_code = chan.recv_exit_status()
    out = b"".join(out_chunks).decode("utf-8", errors="replace").strip()
    err = b"".join(err_chunks).decode("utf-8", errors="replace").strip()
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


def make_machine_key(host: str, username: str, machine_key_seed: str | None = None) -> str:
    """Stable identity key for a RunPod machine."""
    if machine_key_seed:
        raw = f"runpod|seed|{machine_key_seed.strip()}"
    else:
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

    # Install Blender runtime prerequisites on Debian/Ubuntu images.
    deps_cmd = (
        "if command -v apt-get >/dev/null 2>&1; then "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -y >/dev/null 2>&1 && "
        "apt-get install -y --no-install-recommends "
        "ca-certificates wget curl xz-utils unzip "
        "libx11-6 libxi6 libxxf86vm1 libxrender1 libxfixes3 "
        "libxkbcommon0 libxrandr2 libxinerama1 libxcursor1 "
        "libgl1 libegl1 libsm6 libice6 libglib2.0-0 libdbus-1-3 "
        "libfontconfig1 libfreetype6 >/dev/null 2>&1; "
        "fi"
    )
    dep_rc, _, dep_err = ssh_run(client, deps_cmd, timeout=600)
    if dep_rc != 0:
        log(label, f"Dependency install command failed (continuing): {dep_err}", level="warn")

    # Download + extract
    download_cmd = (
        f"cd {REMOTE_BASE} && "
        f"rm -rf blender {BLENDER_DIR} && "
        f"(wget -q --show-progress '{BLENDER_URL}' -O {BLENDER_ARCHIVE} "
        f"|| curl -fL '{BLENDER_URL}' -o {BLENDER_ARCHIVE}) && "
        f"tar xf {BLENDER_ARCHIVE} && "
        f"mv {BLENDER_DIR} blender && "
        f"rm {BLENDER_ARCHIVE}"
    )
    rc, out, err = ssh_run(client, download_cmd, timeout=600)
    if rc != 0:
        detail = err or out or "unknown download/extract error"
        log(label, f"Failed to install Blender: {detail}", level="error")
        return False

    # Verify
    rc, out, err = ssh_run(client, f"{REMOTE_BLENDER}/blender --version")
    if rc != 0:
        detail = err or out or "blender --version failed with no stderr"
        log(label, f"Blender installation verification failed: {detail}", level="error")
        # Extra diagnostics for missing shared libs.
        _, ldd_out, ldd_err = ssh_run(client, f"ldd {REMOTE_BLENDER}/blender | head -n 30")
        diag = ldd_out or ldd_err
        if diag:
            log(label, f"Blender ldd diagnostics: {diag}", level="warn")
        return False

    log(label, f"Blender installed: {out.splitlines()[0] if out else '?'}")
    return True


def upload_render_scripts(client: paramiko.SSHClient, label: str) -> bool:
    """Upload render scripts to the remote machine."""
    sftp = client.open_sftp()
    try:
        ssh_run(client, f"mkdir -p {REMOTE_SCRIPTS}")

        # Upload render.sh
        if RENDER_SH.exists():
            sftp.put(str(RENDER_SH), f"{REMOTE_SCRIPTS}/render.sh")
            ssh_run(client, f"sed -i 's/\\r$//' {REMOTE_SCRIPTS}/render.sh && chmod +x {REMOTE_SCRIPTS}/render.sh")
        else:
            log(label, f"render.sh not found at {RENDER_SH}, writing inline", level="warn")
            _write_inline_render_sh(client)

        # Upload progress_handler.py
        if PROGRESS_HANDLER.exists():
            sftp.put(str(PROGRESS_HANDLER), f"{REMOTE_SCRIPTS}/progress_handler.py")
        else:
            log(label, f"progress_handler.py not found at {PROGRESS_HANDLER}, writing inline", level="warn")
            _write_inline_progress_handler(client)

        # Upload render_driver.py
        if RENDER_DRIVER.exists():
            sftp.put(str(RENDER_DRIVER), f"{REMOTE_SCRIPTS}/render_driver.py")
        else:
            log(label, f"render_driver.py not found at {RENDER_DRIVER}, writing inline", level="warn")
            _write_inline_render_driver(client)

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
BLENDER_BIN="${BLENDER_BIN:-/tmp/pcrent/blender/blender}"
INPUT_DIR="${INPUT_DIR:-/tmp/pcrent/jobs/input}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/pcrent/jobs/output}"
PROGRESS_SCRIPT="${PROGRESS_SCRIPT:-/tmp/pcrent/scripts/progress_handler.py}"
RENDER_DRIVER_SCRIPT="${RENDER_DRIVER_SCRIPT:-/tmp/pcrent/scripts/render_driver.py}"
DEVICE_POLICY="${DEVICE_POLICY:-AUTO}"
FRAME_STEP="${FRAME_STEP:-1}"
BLEND_FILE="${BLEND_FILE:-}"
DEVICE_POLICY="$(echo "$DEVICE_POLICY" | tr '[:lower:]' '[:upper:]')"
if ! [[ "$FRAME_STEP" =~ ^[0-9]+$ ]] || [ "$FRAME_STEP" -lt 1 ]; then FRAME_STEP=1; fi
export OUTPUT_DIR INPUT_DIR FRAME_STEP DEVICE_POLICY
if [ -z "$BLEND_FILE" ]; then BLEND_FILE=$(find "$INPUT_DIR" -name "*.blend" -print -quit); fi
if [ -z "$BLEND_FILE" ] || [ ! -f "$BLEND_FILE" ]; then echo "ERROR: No .blend file found in $INPUT_DIR"; exit 1; fi
run_render() {
  local device="$1" label="$2" log_file="$3"
  local -a cmd=("$BLENDER_BIN" -b "$BLEND_FILE" -P "$PROGRESS_SCRIPT" -P "$RENDER_DRIVER_SCRIPT")
  if [ -n "$device" ]; then cmd+=(-- --cycles-device "$device"); fi
  echo "Rendering with: $label"
  set +e
  "${cmd[@]}" 2>&1 | tee "$log_file"
  local rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}
contains_unavailable() {
  local log_file="$1"
  grep -q "Found no Cycles device of the specified type" "$log_file" || \
  grep -q "Requested Cycles device not available" "$log_file"
}
attempt_render() {
  local log_file
  log_file=$(mktemp)
  local last_exit=0
  case "$DEVICE_POLICY" in
    AUTO)
      if nvidia-smi >/dev/null 2>&1; then
        for device in CUDA OPTIX; do
          if run_render "$device" "$device (GPU)" "$log_file"; then rm -f "$log_file"; return 0; fi
          last_exit=$?
          if contains_unavailable "$log_file"; then : > "$log_file"; continue; fi
          rm -f "$log_file"; return "$last_exit"
        done
      fi
      if run_render "" "CPU" "$log_file"; then rm -f "$log_file"; return 0; fi
      last_exit=$?
      rm -f "$log_file"
      return "$last_exit"
      ;;
    CPU)
      if run_render "" "CPU (strict)" "$log_file"; then rm -f "$log_file"; return 0; fi
      last_exit=$?
      rm -f "$log_file"
      return "$last_exit"
      ;;
    OPTIX|CUDA)
      if ! nvidia-smi >/dev/null 2>&1; then
        echo "ERROR: Requested device '$DEVICE_POLICY', but GPU is not accessible."
        rm -f "$log_file"
        return 2
      fi
      if run_render "$DEVICE_POLICY" "$DEVICE_POLICY (GPU, strict)" "$log_file"; then rm -f "$log_file"; return 0; fi
      last_exit=$?
      if contains_unavailable "$log_file"; then
        echo "ERROR: Requested Cycles device '$DEVICE_POLICY' is unavailable on this worker."
        rm -f "$log_file"
        return 3
      fi
      rm -f "$log_file"
      return "$last_exit"
      ;;
    *)
      echo "ERROR: Unsupported DEVICE_POLICY '$DEVICE_POLICY'"
      rm -f "$log_file"
      return 2
      ;;
  esac
}
attempt_render
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


def _write_inline_render_driver(client: paramiko.SSHClient):
    """Fallback: write render_driver.py inline if the local file is missing."""
    script = '''import base64, json, os, re, sys
import bpy
PFX = "PCR_PROGRESS "
def _ci(v, mn=None, mx=None):
    if v is None: return None
    try: n = int(v)
    except Exception: return None
    if mn is not None and n < mn: n = mn
    if mx is not None and n > mx: n = mx
    return n
def _cb(v):
    if isinstance(v, bool): return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in {"1","true","yes","on"}: return True
        if t in {"0","false","no","off"}: return False
    if isinstance(v, (int,float)): return bool(v)
    return None
def _ov():
    raw = os.environ.get("RENDER_OVERRIDES_JSON","").strip()
    b64 = os.environ.get("RENDER_OVERRIDES_B64","").strip()
    if not raw and b64:
        try: raw = base64.b64decode(b64).decode("utf-8")
        except Exception: raw = ""
    if not raw: return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
def _cam(name):
    if not name: return None
    o = bpy.data.objects.get(name)
    if o is None or getattr(o, "type", None) != "CAMERA": return None
    return o
def _emit(kind, cur, rf, tf):
    print(PFX + json.dumps({"kind": kind, "current_frame": int(cur), "rendered_frames": int(rf), "total_frames": int(tf)}, sort_keys=True), flush=True)
def _p(path, frame):
    m = re.search(r"#+", path or "")
    if m:
        w = m.end() - m.start()
        return f"{path[:m.start()]}{str(frame).zfill(w)}{path[m.end():]}"
    if (path or "").endswith("/") or (path or "").endswith("\\\\"):
        return f"{path}frame{frame:04d}"
    return f"{path}{frame:04d}"
def _apply(scene, ov):
    tl = ov.get("timeline") if isinstance(ov.get("timeline"), dict) else {}
    fs = _ci(os.environ.get("FRAME_START"), 1) or _ci(tl.get("frame_start"), 1) or int(scene.frame_start)
    fe = _ci(os.environ.get("FRAME_END"), 1) or _ci(tl.get("frame_end"), 1) or int(scene.frame_end)
    st = _ci(os.environ.get("FRAME_STEP"), 1) or _ci(tl.get("frame_step"), 1) or int(scene.frame_step or 1)
    if fe < fs: raise RuntimeError(f"Invalid frame range {fs}..{fe}")
    scene.frame_start, scene.frame_end, scene.frame_step = int(fs), int(fe), int(max(1, st))
    out = ov.get("output") if isinstance(ov.get("output"), dict) else {}
    path = out.get("path_pattern")
    if not isinstance(path, str) or not path.strip():
        path = f"{os.environ.get('OUTPUT_DIR','/output').rstrip('/')}/frame####"
    scene.render.filepath = path
    r = ov.get("render") if isinstance(ov.get("render"), dict) else {}
    eng = r.get("engine")
    if isinstance(eng, str) and eng: scene.render.engine = eng
    rx, ry = _ci(r.get("resolution_x"), 1), _ci(r.get("resolution_y"), 1)
    rp = _ci(r.get("resolution_percentage"), 1, 1000)
    if rx is not None: scene.render.resolution_x = rx
    if ry is not None: scene.render.resolution_y = ry
    if rp is not None: scene.render.resolution_percentage = rp
    cyc = getattr(scene, "cycles", None)
    if cyc is not None:
        smp = _ci(r.get("cycles_samples"), 1)
        if smp is not None: cyc.samples = smp
        ads, dns = _cb(r.get("cycles_adaptive_sampling")), _cb(r.get("cycles_denoise"))
        if ads is not None: cyc.use_adaptive_sampling = ads
        if dns is not None: cyc.use_denoising = dns
        pol = r.get("device_policy")
        if isinstance(pol, str):
            p = pol.strip().upper()
            if p == "CPU": cyc.device = "CPU"
            elif p in {"AUTO","CUDA","OPTIX"}: cyc.device = "GPU"
    mode = ov.get("camera_mode") if isinstance(ov.get("camera_mode"), str) else "auto_markers"
    c = _cam(ov.get("camera_name") if isinstance(ov.get("camera_name"), str) else "")
    if mode == "force_camera":
        if c is None: raise RuntimeError("force_camera requires valid camera_name")
        scene.camera = c
        for m in scene.timeline_markers:
            try: m.camera = c
            except Exception: pass
    elif c is not None:
        scene.camera = c
    return mode
def _render_ranges(scene, layer, ov):
    rows = []
    for i, row in enumerate(ov.get("camera_ranges") if isinstance(ov.get("camera_ranges"), list) else []):
        if not isinstance(row, dict): continue
        if _cb(row.get("enabled")) is False: continue
        cam = _cam(row.get("camera_name"))
        if cam is None: raise RuntimeError(f"camera_ranges[{i}] camera not found")
        rs, re = _ci(row.get("frame_start"), 1), _ci(row.get("frame_end"), 1)
        rst = _ci(row.get("frame_step"), 1) or 1
        if rs is None or re is None or re < rs: raise RuntimeError(f"camera_ranges[{i}] invalid frame range")
        rows.append({"cam": cam, "start": rs, "end": re, "step": rst})
    if not rows: raise RuntimeError("camera_ranges mode requires enabled rows")
    rows.sort(key=lambda x: (x["start"], x["end"]))
    frames = list(range(int(scene.frame_start), int(scene.frame_end) + 1, max(1, int(scene.frame_step))))
    if not frames: raise RuntimeError("No frames to render")
    fallback = scene.camera
    assignments = []
    for f in frames:
        picked = None
        for r in rows:
            if f < r["start"] or f > r["end"]: continue
            if (f - r["start"]) % r["step"] == 0:
                picked = r["cam"]
                break
        if picked is None: picked = fallback
        if picked is None: raise RuntimeError(f"No camera resolved for frame {f}")
        assignments.append((f, picked))
    for hl in (bpy.app.handlers.render_init, bpy.app.handlers.render_write):
        for h in list(hl):
            if getattr(h, "__name__", "") in {"on_render_init","on_render_write"}:
                try: hl.remove(h)
                except Exception: pass
    _emit("meta", assignments[0][0], 0, len(assignments))
    base = scene.render.filepath
    kw = dict(write_still=True, scene=scene.name, use_viewport=False)
    if layer and any(vl.name == layer for vl in scene.view_layers):
        kw["layer"] = layer
    try:
        for idx, (f, cam) in enumerate(assignments, start=1):
            scene.camera = cam
            scene.frame_set(f)
            scene.render.filepath = _p(base, f)
            bpy.ops.render.render(**kw)
            _emit("frame", f, idx, len(assignments))
    finally:
        scene.render.filepath = base
def main():
    ov = _ov()
    sn = ov.get("scene_name") if isinstance(ov.get("scene_name"), str) else ""
    scene = bpy.data.scenes.get(sn) if sn else bpy.context.scene
    if scene is None: raise RuntimeError("No renderable scene found")
    mode = _apply(scene, ov)
    layer = ov.get("view_layer") if isinstance(ov.get("view_layer"), str) else ""
    if mode == "camera_ranges":
        _render_ranges(scene, layer, ov)
    else:
        kw = dict(animation=True, scene=scene.name, write_still=False, use_viewport=False)
        if layer and any(vl.name == layer for vl in scene.view_layers):
            kw["layer"] = layer
        bpy.ops.render.render(**kw)
if __name__ == "__main__":
    try: main()
    except Exception as e:
        print(f"[RENDER_DRIVER] ERROR: {e}", file=sys.stderr, flush=True)
        raise
'''
    _, stdin, _ = client.exec_command(f"cat > {REMOTE_SCRIPTS}/render_driver.py")
    stdin.write(script)
    stdin.channel.shutdown_write()


# -----------------------------------------------
# GPU ENVIRONMENT (remote)
# -----------------------------------------------
def build_gpu_env(client: paramiko.SSHClient) -> str:
    """Build an env-prefix string with correct LD_LIBRARY_PATH for CUDA/OptiX.

    Finds the real libcuda.so (skipping /usr/local/cuda/lib64/stubs which is
    a compile-time stub that causes 'cuInit: Unknown CUDA error').
    """
    rc, cuda_paths, _ = ssh_run(
        client,
        "find /usr/lib /usr/local/nvidia /usr/local/cuda/compat -name 'libcuda.so*' "
        "-not -path '*/stubs/*' 2>/dev/null | head -5",
        timeout=10,
    )
    extra_lib_dirs = set()
    for p in (cuda_paths or "").strip().splitlines():
        d = p.rsplit("/", 1)[0]
        if d:
            extra_lib_dirs.add(d)

    ld_path_parts = sorted(extra_lib_dirs) + [
        "/usr/local/nvidia/lib",
        "/usr/local/nvidia/lib64",
        "/usr/local/cuda/compat",
        "/usr/local/cuda/lib64",   # libnvrtc.so — required for OptiX kernel compilation
    ]
    ld_path = ":".join(ld_path_parts) + ":${LD_LIBRARY_PATH:-}"

    return (
        f"LD_LIBRARY_PATH={ld_path} "
        "PATH=/usr/local/nvidia/bin:/usr/local/cuda/bin:$PATH "
        "NVIDIA_VISIBLE_DEVICES=all "
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility"
    )


def verify_blender_gpu(client: paramiko.SSHClient, label: str) -> bool:
    """Verify Blender can detect and use GPU devices via Cycles."""
    gpu_test_py = (
        "import bpy, json, sys\n"
        "result = {\"devices\": [], \"type\": None}\n"
        "try:\n"
        "    prefs = bpy.context.preferences.addons[\"cycles\"].preferences\n"
        "    for ctype in (\"OPTIX\", \"CUDA\"):\n"
        "        try:\n"
        "            prefs.compute_device_type = ctype\n"
        "            try:\n"
        "                prefs.get_devices()\n"
        "            except Exception:\n"
        "                try:\n"
        "                    prefs.refresh_devices()\n"
        "                except Exception:\n"
        "                    pass\n"
        "            devs = [(d.name, d.type) for d in getattr(prefs, \"devices\", []) if d.type != \"CPU\"]\n"
        "            if devs:\n"
        "                result[\"devices\"] = devs\n"
        "                result[\"type\"] = ctype\n"
        "                break\n"
        "        except Exception:\n"
        "            continue\n"
        "except Exception as e:\n"
        "    result[\"error\"] = str(e)\n"
        "print(\"GPU_TEST:\" + json.dumps(result))\n"
        "sys.exit(0 if result[\"devices\"] else 1)\n"
    )
    # Write test script via heredoc to avoid shell quoting issues
    write_cmd = f"cat << 'PYEOF' > /tmp/pcrent/gpu_test.py\n{gpu_test_py}PYEOF"
    ssh_run(client, write_cmd, timeout=10)

    gpu_env = build_gpu_env(client)

    # Initialize CUDA driver (required on some RunPod containers)
    ssh_run(client, "nvidia-smi > /dev/null 2>&1", timeout=15)

    rc, out, err = ssh_run(
        client,
        f"{gpu_env} {REMOTE_BLENDER}/blender -b --factory-startup -P /tmp/pcrent/gpu_test.py 2>&1",
        timeout=120,
    )

    for line in (out or "").splitlines():
        if line.startswith("GPU_TEST:"):
            try:
                result = json.loads(line[len("GPU_TEST:"):])
                if result.get("devices"):
                    devices = result["devices"]
                    ctype = result.get("type", "?")
                    log(label, f"Blender GPU check OK: {ctype} devices: {[d[0] for d in devices]}")
                    return True
                if result.get("error"):
                    log(label, f"Blender GPU check error: {result['error']}", level="error")
            except json.JSONDecodeError:
                pass

    log(label, f"Blender GPU check failed: no GPU devices found. Output: {(out or err or '')[-500:]}", level="error")
    return False


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
    for batch_idx, (batch, batch_bytes) in enumerate(batches, start=1):
        streams: list[io.BytesIO] = []
        file_tuples = []
        try:
            for filename, data in batch:
                stream = io.BytesIO(data)
                streams.append(stream)
                file_tuples.append(("files", (filename, stream)))

            timeout = max(120, min(900, 60 + int(batch_bytes / (1024 * 1024)) * 10))
            log(
                "UPLOAD",
                f"[JOB {job_id[:8]}] Uploading batch {batch_idx}/{len(batches)} "
                f"({len(batch)} files, {batch_bytes / (1024 * 1024):.1f} MB)...",
            )
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    resp = requests.post(
                        f"{BACKEND_URL}/jobs/{job_id}/output",
                        files=file_tuples,
                        timeout=(20, timeout),
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
                    last_exc = None
                    break
                except requests.RequestException as exc:
                    last_exc = exc
                    if attempt >= 3:
                        break
                    wait_s = attempt * 2
                    log(
                        "UPLOAD",
                        f"[JOB {job_id[:8]}] Batch {batch_idx} upload attempt {attempt}/3 failed: {exc}. "
                        f"Retrying in {wait_s}s...",
                        level="warn",
                    )
                    time.sleep(wait_s)

            if last_exc is not None:
                raise RuntimeError(
                    f"Failed to upload output batch {batch_idx}/{len(batches)} after retries: {last_exc}"
                ) from last_exc
        finally:
            for stream in streams:
                stream.close()

        uploaded_names.extend(filename for filename, _ in batch)

    return uploaded_names


def download_input_file(
    url: str,
    stop_event: threading.Event | None = None,
    on_progress=None,
    max_seconds: int = 1800,
) -> bytes:
    started_at = time.monotonic()
    resp = requests.get(url, stream=True, timeout=(30, 120))
    resp.raise_for_status()
    buf = io.BytesIO()
    downloaded = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if stop_event and stop_event.is_set():
            raise RuntimeError("Stop requested during input download")
        if (time.monotonic() - started_at) > max_seconds:
            raise RuntimeError(f"Input download exceeded {max_seconds}s timeout")
        if not chunk:
            continue
        buf.write(chunk)
        downloaded += len(chunk)
        if on_progress:
            try:
                on_progress(downloaded)
            except Exception:
                pass
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


def parse_job_render_overrides(job: dict) -> dict:
    raw = job.get("render_overrides_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


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
            message = str(e)
            if " 409 " in f" {message} " or "Conflict" in message:
                # Job can transition state before final progress push on failures.
                return
            log(label, f"Progress push failed: {e}", level="warn")

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        # 1. Create remote job dirs
        ssh_run(client, f"mkdir -p {remote_input_dir} {remote_output_dir}")

        # 2. Download blend file locally, SFTP it over
        log(label, f"[JOB {job_id[:8]}] Downloading {input_filename} from backend...")
        dl_started = time.monotonic()
        last_dl_log_at = {"t": 0.0}
        last_dl_mb = {"mb": 0}

        def on_download_progress(downloaded_bytes: int):
            now = time.monotonic()
            downloaded_mb = downloaded_bytes // (1024 * 1024)
            if downloaded_mb >= last_dl_mb["mb"] + 25 or (now - last_dl_log_at["t"]) >= 10:
                last_dl_mb["mb"] = downloaded_mb
                last_dl_log_at["t"] = now
                log(label, f"[JOB {job_id[:8]}] Downloaded {downloaded_mb} MB...")

        input_data = download_input_file(
            input_url,
            stop_event=stop_event,
            on_progress=on_download_progress,
            max_seconds=1800,
        )
        log(
            label,
            f"[JOB {job_id[:8]}] Download complete: {len(input_data) // (1024 * 1024)} MB "
            f"in {int(time.monotonic() - dl_started)}s"
        )

        sftp = client.open_sftp()
        try:
            remote_input_path = f"{remote_input_dir}/{input_filename}"
            log(label, f"[JOB {job_id[:8]}] Uploading input to remote worker...")
            try:
                sftp_channel = sftp.get_channel()
                sftp_channel.settimeout(120)
            except Exception:
                pass

            total_bytes = len(input_data)
            total_mb = max(1, total_bytes // (1024 * 1024))
            upload_state = {
                "last_log_at": 0.0,
                "last_log_bytes": 0,
            }

            def on_sftp_progress(transferred: int, total: int):
                if stop_event.is_set():
                    raise RuntimeError("Stop requested during input upload")
                now = time.monotonic()
                bytes_delta = transferred - upload_state["last_log_bytes"]
                should_log = (
                    bytes_delta >= (5 * 1024 * 1024)
                    or (now - upload_state["last_log_at"]) >= 10.0
                    or transferred >= total
                )
                if should_log:
                    upload_state["last_log_at"] = now
                    upload_state["last_log_bytes"] = transferred
                    done_mb = transferred // (1024 * 1024)
                    pct = int((transferred / total) * 100) if total else 0
                    log(label, f"[JOB {job_id[:8]}] Remote upload {done_mb}/{total_mb} MB ({pct}%)")

            try:
                sftp.putfo(
                    io.BytesIO(input_data),
                    remote_input_path,
                    file_size=total_bytes,
                    callback=on_sftp_progress,
                    confirm=True,
                )
            except Exception as exc:
                raise RuntimeError(f"Failed to upload input to remote worker: {exc}") from exc
            log(label, f"[JOB {job_id[:8]}] Remote upload complete")

            # Handle ZIP: extract on remote
            if input_filename.lower().endswith(".zip"):
                log(label, f"[JOB {job_id[:8]}] Extracting archive...")
                quoted_input = shlex.quote(input_filename)
                extract_cmd = (
                    f"cd {shlex.quote(remote_input_dir)} && "
                    f"if command -v unzip >/dev/null 2>&1; then "
                    f"unzip -o {quoted_input} && rm {quoted_input}; "
                    f"elif command -v python3 >/dev/null 2>&1; then "
                    f"python3 -m zipfile -e {quoted_input} . && rm {quoted_input}; "
                    f"else "
                    f"echo 'No unzip or python3 available to extract ZIP' >&2; "
                    f"exit 1; "
                    f"fi"
                )
                rc, out, err = ssh_run(
                    client,
                    extract_cmd,
                    timeout=900,
                )
                if rc != 0:
                    detail = err or out or "unzip failed with unknown error"
                    raise RuntimeError(f"Failed to extract ZIP: {detail}")
                log(label, f"[JOB {job_id[:8]}] Archive extracted")
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

        # 4. Build direct render command
        render_overrides = parse_job_render_overrides(job)
        overrides_json = json.dumps(render_overrides, separators=(",", ":"), ensure_ascii=True)
        overrides_b64 = base64.b64encode(overrides_json.encode("utf-8")).decode("ascii")

        frame_step = job.get("frame_step") or 1
        try:
            frame_step = max(1, int(frame_step))
        except (TypeError, ValueError):
            frame_step = 1

        device_policy = "AUTO"
        render_block = render_overrides.get("render")
        if isinstance(render_block, dict):
            value = render_block.get("device_policy")
            if isinstance(value, str) and value.strip():
                device_policy = value.strip().upper()

        gpu_env = build_gpu_env(client)

        env_parts = [
            f"BLENDER_BIN={REMOTE_BLENDER}/blender",
            f"INPUT_DIR={shlex.quote(remote_input_dir)}",
            f"OUTPUT_DIR={shlex.quote(remote_output_dir)}",
            f"PROGRESS_SCRIPT={REMOTE_SCRIPTS}/progress_handler.py",
            f"RENDER_DRIVER_SCRIPT={REMOTE_SCRIPTS}/render_driver.py",
            f"DEVICE_POLICY={device_policy}",
            f"FRAME_STEP={frame_step}",
            f"BLEND_FILE={shlex.quote(blend_path)}",
            f"RENDER_OVERRIDES_B64={overrides_b64}",
            gpu_env,
        ]

        if job.get("frame_start") is not None and job.get("frame_end") is not None:
            env_parts.append(f"FRAME_START={int(job['frame_start'])}")
            env_parts.append(f"FRAME_END={int(job['frame_end'])}")
            log(label, f"[JOB {job_id[:8]}] Distributed render: frames {job['frame_start']}-{job['frame_end']} step {frame_step}")

        render_cmd = " ".join(env_parts) + f" {REMOTE_SCRIPTS}/render.sh 2>&1"

        render_log = []

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
                render_log.append(line)
                log(label, line)

        log(label, f"[JOB {job_id[:8]}] Starting Blender render...")
        log(label, f"[JOB {job_id[:8]}] Command: {render_cmd}")
        exit_code = ssh_run_stream(client, render_cmd, on_line=on_line, stop_event=stop_event)

        if stop_event.is_set():
            raise RuntimeError("Stop requested")

        missing_assets = any(m in "\n".join(render_log) for m in MISSING_LIBRARY_MARKERS)

        if exit_code != 0:
            if missing_assets:
                raise RuntimeError(
                    "Project references external assets not uploaded. "
                    "Use a packed .blend or .zip bundle."
                )
            raise RuntimeError(f"Render exited with code {exit_code}")

        # 5. Download output files via SFTP
        log(label, f"[JOB {job_id[:8]}] Collecting output files...")
        rc, file_list, err = ssh_run(client, f"ls -1 {shlex.quote(remote_output_dir)}", timeout=30)
        if rc != 0 or not file_list.strip():
            detail = err or file_list
            if missing_assets:
                raise RuntimeError("Project references external assets. No output produced.")
            raise RuntimeError(f"Render produced no output files. ls output: {detail}")

        output_filenames = [f for f in file_list.strip().splitlines() if not f.startswith(".")]
        if not output_filenames:
            raise RuntimeError("Render produced no output files")

        log(label, f"[JOB {job_id[:8]}] Found {len(output_filenames)} output files on worker")
        sftp = client.open_sftp()
        output_files_data = []
        try:
            try:
                sftp_channel = sftp.get_channel()
                sftp_channel.settimeout(120)
            except Exception:
                pass
            for fname in output_filenames:
                remote_path = f"{remote_output_dir}/{fname}"
                log(label, f"[JOB {job_id[:8]}] Downloading output from worker: {fname}")
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
        self.machine_key_seed = cfg.get("machine_key_seed")

        self.machine_id: str | None = None
        self.client: paramiko.SSHClient | None = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._job_stop_event = threading.Event()
        self._obs_lock = threading.Lock()
        self._state = "connecting"
        self._state_changed_at = time.time()
        self._last_available_at: float | None = None
        self._last_job_started_at: float | None = None
        self._last_job_finished_at: float | None = None
        self._last_error: str | None = None

    def _set_state(self, state: str, *, error: str | None = None):
        now = time.time()
        with self._obs_lock:
            self._state = state
            self._state_changed_at = now
            if state == "available":
                self._last_available_at = now
            if state == "running":
                self._last_job_started_at = now
            if state == "error":
                self._last_error = error or self._last_error

    def _mark_job_finished(self):
        now = time.time()
        with self._obs_lock:
            self._last_job_finished_at = now

    def get_observability_snapshot(self) -> dict:
        with self._obs_lock:
            return {
                "state": self._state,
                "state_changed_at": self._state_changed_at,
                "last_available_at": self._last_available_at,
                "last_job_started_at": self._last_job_started_at,
                "last_job_finished_at": self._last_job_finished_at,
                "last_error": self._last_error,
                "machine_id": self.machine_id,
                "host": self.host,
                "port": self.port,
                "label": self.label,
            }

    def _connect(self) -> bool:
        try:
            self._set_state("connecting")
            log(self.label, f"Connecting to {self.username}@{self.host}:{self.port}...")
            self.client = make_ssh_client(
                self.host, self.port, self.username,
                self.key_path, self.password
            )
            try:
                transport = self.client.get_transport()
                if transport:
                    transport.set_keepalive(30)
            except Exception:
                pass
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
                self._set_state("error", error="SSH connection failed")
                log(self.label, "Retrying in 30s...")
                self.stop_event.wait(30)
                continue

            try:
                self._set_state("setup")
                self._setup_and_poll()
            except Exception as e:
                self._set_state("error", error=str(e))
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
        self._set_state("stopping")

    def _setup_and_poll(self):
        # 1. Install Blender directly on the remote machine
        if not ensure_blender(self.client, self.label):
            raise RuntimeError("Blender installation failed")

        # 2. Upload render scripts (render.sh, render_driver.py, progress_handler.py)
        if not upload_render_scripts(self.client, self.label):
            raise RuntimeError("Failed to upload render scripts")

        # 3. Verify Blender can access GPU via Cycles
        if not verify_blender_gpu(self.client, self.label):
            raise RuntimeError("Blender GPU validation failed")

        # 4. Detect specs and register
        machine_key = make_machine_key(self.host, self.username, self.machine_key_seed)
        log(self.label, "Detecting hardware specs...")
        specs = detect_remote_specs(self.client, machine_key)
        log(self.label, f"  GPU: {specs['gpu_model']} ({specs['gpu_vram_gb']} GB VRAM)")
        log(self.label, f"  CPU: {specs['cpu_cores']} cores  RAM: {specs['ram_gb']} GB")
        log(self.label, f"  OS:  {specs['os_version']}")

        log(self.label, "Registering with backend...")
        self.machine_id = register_machine(specs)
        log(self.label, f"Machine ID: {self.machine_id}")

        set_available(self.machine_id)
        self._set_state("available")
        log(self.label, "Available. Polling for jobs...")

        # 5. Poll loop
        while not self.stop_event.is_set():
            if not self._is_connected():
                log(self.label, "SSH connection lost, reconnecting...")
                if not self._reconnect():
                    raise RuntimeError("Could not reconnect via SSH")
                set_available(self.machine_id)
                self._set_state("available")

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
                    self._set_state("running")
                    update_job_status(job["id"], "running")
                    execute_job(
                        self.client, job, self.machine_id, self.label,
                        self._job_stop_event, self.pause_event
                    )
                except Exception as e:
                    self._set_state("error", error=str(e))
                    log(self.label, f"Job execution error: {e}", level="error")
                    try:
                        update_job_status(job["id"], "failed", error=str(e))
                    except Exception:
                        pass
                finally:
                    self._mark_job_finished()

                if not self.stop_event.is_set() and not self.pause_event.is_set():
                    set_available(self.machine_id)
                    self._set_state("available")
                    log(self.label, "Back to polling...")
            else:
                time.sleep(POLL_INTERVAL)

    def stop(self):
        self._set_state("stopping")
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
