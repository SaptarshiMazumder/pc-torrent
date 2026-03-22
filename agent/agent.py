"""
PC Rent - Desktop Agent
Runs on provider's Windows PC. Detects specs, registers with backend, polls for jobs, executes them.
"""

import os
import sys
import time
import json
import platform
import subprocess
import tempfile
import shutil
import signal
import threading
import hashlib
import socket
import uuid
import requests

# -----------------------------------------------
# CONFIG
# -----------------------------------------------
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
POLL_INTERVAL = 5  # seconds between job polls
BLENDER_PATH = os.environ.get("BLENDER_PATH", r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe")
USE_SANDBOX = os.environ.get("USE_SANDBOX", "false").lower() == "true"
MOCK_MODE = os.environ.get("MOCK_MODE", "false").lower() == "true"  # Test without Blender

machine_id = None
running = True

# -----------------------------------------------
# HARDWARE DETECTION
# -----------------------------------------------
def get_machine_key():
    """
    Build a stable identity for this physical machine so server can dedupe registrations.
    """
    parts = []

    # Windows machine GUID is usually stable across agent restarts.
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if machine_guid:
                parts.append(str(machine_guid))
    except Exception:
        pass

    host = os.environ.get("COMPUTERNAME") or socket.gethostname()
    if host:
        parts.append(host)

    # Usually real MAC. If unavailable, Python may synthesize one.
    parts.append(str(uuid.getnode()))

    if not parts:
        parts.append(platform.node() or "unknown-machine")

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_gpu_info():
    """Try nvidia-smi first, fall back to wmic."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            name, vram = lines[0].split(", ")
            return name.strip(), round(int(vram.strip()) / 1024, 1)
    except Exception:
        pass

    # Try wmic for AMD/other
    try:
        result = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip() and "Name" not in l]
        if lines:
            parts = lines[0].rsplit(" ", 1)
            name = parts[0].strip()
            vram_bytes = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0
            return name, round(vram_bytes / (1024**3), 1)
    except Exception:
        pass

    return "Unknown GPU", 0.0

def get_cpu_cores():
    return os.cpu_count() or 1

def get_ram_gb():
    try:
        result = subprocess.run(
            ["wmic", "computersystem", "get", "TotalPhysicalMemory"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip().isdigit()]
        if lines:
            return round(int(lines[0]) / (1024**3), 1)
    except Exception:
        pass
    return 0.0

def detect_specs():
    gpu_model, gpu_vram = get_gpu_info()
    cpu_cores = get_cpu_cores()
    ram_gb = get_ram_gb()
    return {
        "machine_key": get_machine_key(),
        "gpu_model": gpu_model,
        "gpu_vram_gb": gpu_vram,
        "cpu_cores": cpu_cores,
        "ram_gb": ram_gb
    }

# -----------------------------------------------
# BACKEND COMMUNICATION
# -----------------------------------------------
def register_machine(specs):
    resp = requests.post(f"{BACKEND_URL}/machines/register", json=specs)
    resp.raise_for_status()
    return resp.json()["machine_id"]

def set_available(mid):
    requests.put(f"{BACKEND_URL}/machines/{mid}/available").raise_for_status()

def set_idle(mid):
    try:
        requests.put(f"{BACKEND_URL}/machines/{mid}/idle")
    except Exception:
        pass

def poll_for_job(mid):
    resp = requests.get(f"{BACKEND_URL}/jobs/next-for-machine/{mid}", timeout=10)
    resp.raise_for_status()
    return resp.json()

def update_job_status(job_id, status, error=None, output_files=None):
    payload = {"status": status}
    if error:
        payload["error"] = error
    if output_files:
        payload["output_files"] = output_files
    requests.put(f"{BACKEND_URL}/jobs/{job_id}/status", json=payload).raise_for_status()

def upload_output_files(job_id, output_dir):
    files_found = [f for f in os.listdir(output_dir) if not f.startswith(".") and f != "RENDER_DONE.txt"]
    if not files_found:
        return []

    file_tuples = [("files", (f, open(os.path.join(output_dir, f), "rb"))) for f in files_found]
    resp = requests.post(f"{BACKEND_URL}/jobs/{job_id}/output", files=file_tuples)
    resp.raise_for_status()

    for _, (_, fobj) in file_tuples:
        fobj.close()

    return files_found

def download_input_file(input_url, dest_path):
    resp = requests.get(input_url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

# -----------------------------------------------
# JOB EXECUTION
# -----------------------------------------------
def run_mock_render(blend_file, output_dir):
    """
    Mock render for testing (no Blender required).
    Creates dummy output images.
    """
    print(f"[EXEC] Mock render mode - creating dummy output")
    # Simulate render time
    time.sleep(3)
    # Create dummy frame files
    for i in range(1, 4):
        frame_file = os.path.join(output_dir, f"frame{i:04d}.png")
        with open(frame_file, "w") as f:
            f.write(f"[Mock PNG frame {i}]")
    print(f"[EXEC] Created 3 dummy frames in {output_dir}")

def run_blender_direct(blend_file, output_dir):
    """
    Run Blender directly on host machine (no sandbox).
    USE_SANDBOX=false (default for MVP).
    """
    if not os.path.exists(BLENDER_PATH):
        raise FileNotFoundError(f"Blender not found at: {BLENDER_PATH}\nSet BLENDER_PATH env var.")

    output_pattern = os.path.join(output_dir, "frame###")
    cmd = [
        BLENDER_PATH,
        "-b", blend_file,
        "-o", output_pattern,
        "-a"  # render all frames
    ]
    print(f"[EXEC] Running Blender: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, timeout=3600)  # 1 hour max
    if result.returncode != 0:
        raise RuntimeError(f"Blender exited with code {result.returncode}")

def run_blender_sandbox(blend_file, input_dir, output_dir):
    """
    Run Blender inside Windows Sandbox via .wsb config file.
    USE_SANDBOX=true
    """
    blender_dir = os.path.dirname(BLENDER_PATH)
    blender_exe = os.path.basename(BLENDER_PATH)
    blend_name = os.path.basename(blend_file)

    # Batch script that runs inside sandbox
    bat_content = f"""@echo off
"C:\\blender\\{blender_exe}" -b "C:\\input\\{blend_name}" -o "C:\\output\\frame###" -a
echo done > C:\\output\\RENDER_DONE.txt
"""
    bat_path = os.path.join(input_dir, "run_render.bat")
    with open(bat_path, "w") as f:
        f.write(bat_content)

    wsb_config = f"""<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{input_dir}</HostFolder>
      <SandboxFolder>C:\\input</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{output_dir}</HostFolder>
      <SandboxFolder>C:\\output</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{blender_dir}</HostFolder>
      <SandboxFolder>C:\\blender</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>C:\\input\\run_render.bat</Command>
  </LogonCommand>
</Configuration>"""

    wsb_path = os.path.join(tempfile.gettempdir(), "pcrent_job.wsb")
    with open(wsb_path, "w") as f:
        f.write(wsb_config)

    print(f"[EXEC] Launching Windows Sandbox...")
    subprocess.Popen(["WindowsSandbox.exe", wsb_path])

    # Wait for RENDER_DONE.txt to appear in output dir (sandbox signals completion)
    done_file = os.path.join(output_dir, "RENDER_DONE.txt")
    timeout = 3600  # 1 hour max
    waited = 0
    while not os.path.exists(done_file) and waited < timeout:
        time.sleep(5)
        waited += 5
        print(f"[EXEC] Waiting for render... ({waited}s)")

    if not os.path.exists(done_file):
        raise TimeoutError("Render timed out after 1 hour")

    os.remove(done_file)

def execute_job(job):
    job_id = job["id"]
    input_url = job["input_url"]
    input_filename = job["input_filename"]

    work_dir = os.path.join(tempfile.gettempdir(), f"pcrent_{job_id}")
    input_dir = os.path.join(work_dir, "input")
    output_dir = os.path.join(work_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    try:
        # 1. Download blend file
        blend_file = os.path.join(input_dir, input_filename)
        print(f"[JOB] Downloading: {input_filename}")
        download_input_file(input_url, blend_file)

        # 2. Mark job running
        update_job_status(job_id, "running")

        # 3. Run render
        if MOCK_MODE:
            run_mock_render(blend_file, output_dir)
        elif USE_SANDBOX:
            run_blender_sandbox(blend_file, input_dir, output_dir)
        else:
            run_blender_direct(blend_file, output_dir)

        # 4. Upload output files
        print(f"[JOB] Uploading output files...")
        output_files = upload_output_files(job_id, output_dir)

        # 5. Mark done
        update_job_status(job_id, "done", output_files=output_files)
        print(f"[JOB] Done! Files: {output_files}")

    except Exception as e:
        print(f"[JOB] Error: {e}")
        update_job_status(job_id, "failed", error=str(e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

# -----------------------------------------------
# MAIN LOOP
# -----------------------------------------------
def shutdown_handler(sig, frame):
    global running
    print("\n[AGENT] Shutting down...")
    running = False
    if machine_id:
        set_idle(machine_id)
    sys.exit(0)

def main():
    global machine_id, running

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    print("=== PC Rent Agent ===")
    print(f"Backend: {BACKEND_URL}")
    print(f"Mock mode: {MOCK_MODE}")
    print(f"Sandbox mode: {USE_SANDBOX}")
    if not MOCK_MODE:
        print(f"Blender path: {BLENDER_PATH}")
    print()

    # Detect hardware
    print("[AGENT] Detecting hardware specs...")
    specs = detect_specs()
    print(f"  GPU: {specs['gpu_model']} ({specs['gpu_vram_gb']} GB VRAM)")
    print(f"  CPU: {specs['cpu_cores']} cores")
    print(f"  RAM: {specs['ram_gb']} GB")
    print()

    # Register with backend
    print("[AGENT] Registering with backend...")
    try:
        machine_id = register_machine(specs)
        print(f"[AGENT] Registered. Machine ID: {machine_id}")
    except Exception as e:
        print(f"[AGENT] Failed to register: {e}")
        sys.exit(1)

    # Mark available
    set_available(machine_id)
    print("[AGENT] Marked as available. Polling for jobs...\n")

    # Poll loop
    while running:
        try:
            job = poll_for_job(machine_id)
            if job:
                print(f"[AGENT] Got job: {job['id']} ({job['input_filename']})")
                execute_job(job)
                # Re-mark available after job
                set_available(machine_id)
                print("[AGENT] Back to polling...\n")
            else:
                time.sleep(POLL_INTERVAL)
        except requests.exceptions.ConnectionError:
            print(f"[AGENT] Cannot reach backend, retrying in {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            print(f"[AGENT] Unexpected error: {e}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
