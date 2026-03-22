"""
PC Rent - Desktop Agent (v2 - Dockerized GPU Rendering)
Runs on provider's Windows PC. Detects specs, bootstraps Docker,
registers with backend, polls for jobs, executes them in GPU containers.
"""

import os
import sys
import time
import signal
import shutil
import tempfile
import subprocess
import threading
import zipfile
import json
import requests

from config import (
    load_machine_id,
    save_machine_id,
    load_image_sha,
    save_image_sha,
    ensure_config_dir,
)
from system_check import check_requirements, get_windows_version, check_nvidia_gpu
from docker_setup import full_bootstrap, check_docker_running

# -----------------------------------------------
# CONFIG
# -----------------------------------------------
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
POLL_INTERVAL = 5  # seconds between job polls
RENDER_TIMEOUT = 4 * 3600  # 4 hours max per render
DOCKER_IMAGE = "pcrent-render:latest"
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
    files_found = [
        f for f in os.listdir(output_dir)
        if not f.startswith(".") and os.path.isfile(os.path.join(output_dir, f))
    ]
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


def find_blend_files(root_dir):
    blend_files = []
    for current_root, _, files in os.walk(root_dir):
        for name in files:
            if name.lower().endswith(".blend"):
                blend_files.append(os.path.join(current_root, name))
    blend_files.sort()
    return blend_files


def prepare_job_input(downloaded_file, input_dir):
    validate_input_filename(os.path.basename(downloaded_file))

    if downloaded_file.lower().endswith(".zip"):
        print("[JOB] Extracting project archive...")
        safe_extract_zip(downloaded_file, input_dir)
        try:
            os.remove(downloaded_file)
        except OSError:
            pass

    blend_files = find_blend_files(input_dir)
    if not blend_files:
        raise RuntimeError(
            "No .blend file found in the uploaded input. Upload a .blend file or a .zip project bundle "
            "that contains exactly one .blend file."
        )
    if len(blend_files) > 1:
        names = ", ".join(os.path.relpath(path, input_dir) for path in blend_files[:3])
        extra = "" if len(blend_files) <= 3 else ", ..."
        raise RuntimeError(
            "Multiple .blend files were found in the uploaded archive. Keep exactly one render target .blend "
            f"file in the archive. Found: {names}{extra}"
        )

    blend_file = blend_files[0]
    print(f"[JOB] Render target: {os.path.relpath(blend_file, input_dir)}")
    return blend_file


def detect_missing_project_assets(log_lines):
    combined = "\n".join(log_lines)
    return any(marker in combined for marker in MISSING_LIBRARY_MARKERS)


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

    print(f"[JOB] Local output snapshot: {job_output_dir}")
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
                timeout=20,
            )
        except Exception:
            pass

        try:
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
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
        print(f"[AGENT] Stopping active render job: {snapshot['id']}")

    _stop_active_job_runtime(snapshot)
    return True


def should_offer_capacity():
    return running and not pause_event.is_set() and not shutdown_event.is_set()


def pause_agent(reason="Paused by operator"):
    if pause_event.is_set():
        print("[AGENT] Agent is already paused.")
        return

    pause_event.set()
    print(f"[AGENT] {reason}. The machine will stop taking new jobs.")

    if not request_stop_current_job("Render stopped because the machine was paused") and machine_id:
        set_idle(machine_id)


def resume_agent():
    if shutdown_event.is_set():
        print("[AGENT] Cannot resume because shutdown is already in progress.")
        return

    if not pause_event.is_set():
        print("[AGENT] Agent is already available.")
        return

    pause_event.clear()
    if machine_id:
        set_available(machine_id)
    print("[AGENT] Agent resumed and is available for jobs.")


def shutdown_agent(reason="Shutdown requested"):
    global running

    if shutdown_event.is_set():
        return

    print(f"\n[AGENT] {reason}")
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
    """Check if pcrent-render image is loaded in Docker."""
    try:
        result = subprocess.run(
            ["docker", "images", "pcrent-render", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def get_server_image_version():
    """Get the current image version/hash from server."""
    try:
        resp = requests.get(f"{BACKEND_URL}/docker/image/version", timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def ensure_docker_image():
    """
    Make sure the render image is loaded and up to date.
    Downloads from server if needed.
    """
    server_version = get_server_image_version()
    if not server_version:
        # Server doesn't have an image yet - check if we have one locally
        if check_image_loaded():
            print("[IMAGE] Using locally cached image (server has no image info).")
            return True
        print("[IMAGE] No render image available on server or locally.")
        return False

    server_sha = server_version.get("sha256", "")
    local_sha = load_image_sha() or ""

    if check_image_loaded() and server_sha == local_sha:
        print("[IMAGE] Render image is up to date.")
        return True

    # Need to download
    print("[IMAGE] Downloading render image from server...")
    tmp_path = os.path.join(tempfile.gettempdir(), "pcrent-render.tar.gz")

    try:
        resp = requests.get(f"{BACKEND_URL}/docker/image", stream=True, timeout=600)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0

        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = int(downloaded / total * 100)
                    print(f"\r[IMAGE] Downloading... {pct}% ({downloaded // (1024*1024)}MB)", end="", flush=True)

        print()
        print("[IMAGE] Loading image into Docker...")
        result = subprocess.run(
            ["docker", "load", "-i", tmp_path],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            print(f"[IMAGE] Failed to load image: {result.stderr}")
            return False

        print("[IMAGE] Image loaded successfully.")
        save_image_sha(server_sha)

        # Cleanup temp file
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        return True

    except Exception as e:
        print(f"[IMAGE] Failed to download image: {e}")
        return False


# -----------------------------------------------
# JOB EXECUTION (Docker + GPU)
# -----------------------------------------------
def execute_job(job):
    """Execute a render job inside a Docker container with GPU access."""
    job_id = job["id"]
    input_url = job["input_url"]
    input_filename = job["input_filename"]
    final_status = "failed"
    final_error = None

    work_dir = os.path.join(tempfile.gettempdir(), f"pcrent_{job_id}")
    input_dir = os.path.join(work_dir, "input")
    output_dir = os.path.join(work_dir, "output")
    container_name = None
    process = None
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    begin_active_job(job_id, work_dir)

    try:
        # 1. Download blend file
        blend_file = os.path.join(input_dir, input_filename)
        print(f"[JOB] Downloading: {input_filename}")
        download_input_file(input_url, blend_file)
        blend_file = prepare_job_input(blend_file, input_dir)

        stop_reason = get_active_job_stop_reason(job_id)
        if stop_reason:
            raise JobStopped(stop_reason)

        # 2. Mark job running
        update_job_status(job_id, "running")

        # 3. Run Docker container with GPU
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
            DOCKER_IMAGE,
        ]

        print(f"[JOB] Starting Docker render container...")
        print(f"[JOB] Command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        update_active_job(job_id=job_id, process=process)

        # Stream container output
        container_log = []
        try:
            for line in iter(process.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    container_log.append(line)
                    print(f"[CONTAINER] {line}")
        finally:
            if process.stdout:
                process.stdout.close()

        process.wait(timeout=RENDER_TIMEOUT)
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

        # 4. Upload output files
        print(f"[JOB] Uploading output files...")
        output_files = upload_output_files(job_id, output_dir)

        if not output_files:
            if missing_assets:
                raise RuntimeError(
                    "The project references external Blender libraries or assets that were not uploaded. "
                    "No output files were produced."
                )
            raise RuntimeError("Render produced no output files")

        # 5. Mark done
        if missing_assets:
            final_error = MISSING_ASSETS_WARNING
            update_job_status(job_id, "done", error=final_error, output_files=output_files)
            print(f"[JOB] Done with warnings! Files: {output_files}")
            print(f"[JOB] Warning: {final_error}")
        else:
            update_job_status(job_id, "done", output_files=output_files)
            print(f"[JOB] Done! Files: {output_files}")
        final_status = "done"

    except JobStopped as e:
        print(f"[JOB] {e}")
        final_error = str(e)
        update_job_status(job_id, "failed", error=final_error)

    except subprocess.TimeoutExpired:
        print(f"[JOB] Render timed out after {RENDER_TIMEOUT}s, killing container...")
        try:
            subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10)
        except Exception:
            pass
        final_error = "Render timed out"
        update_job_status(job_id, "failed", error=final_error)

    except Exception as e:
        print(f"[JOB] Error: {e}")
        final_error = str(e)
        update_job_status(job_id, "failed", error=final_error)

    finally:
        persist_job_outputs(job_id, output_dir, final_status, final_error)
        clear_active_job(job_id)
        if pause_event.is_set() or shutdown_event.is_set():
            if machine_id:
                set_idle(machine_id)
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

    print("=== PC Rent Agent v2 (Docker GPU) ===")
    print(f"Backend: {BACKEND_URL}")
    print()

    # Step 1: System requirements check
    print("[AGENT] Checking system requirements...")
    req = check_requirements()
    print(f"  OS:     {req['os_version']}")
    if req["gpu_name"]:
        print(f"  GPU:    {req['gpu_name']} ({req['gpu_vram_gb']} GB VRAM)")
        print(f"  Driver: {req['nvidia_driver']}")
    print()

    if not req["ready"]:
        print("[AGENT] System requirements not met:")
        for issue in req["issues"]:
            for line in issue.split("\n"):
                print(f"  {line}")
        print()
        print("[AGENT] Please fix the above issues and restart the agent.")
        input("Press Enter to exit...")
        sys.exit(1)

    # Step 2: Docker bootstrap
    print("[AGENT] Setting up Docker...")
    docker_result = full_bootstrap()

    if docker_result.get("needs_reboot"):
        print()
        print(f"[AGENT] {docker_result['message']}")
        input("Press Enter to exit...")
        sys.exit(0)

    if not docker_result.get("ready"):
        print(f"[AGENT] Docker setup failed: {docker_result['message']}")
        input("Press Enter to exit...")
        sys.exit(1)

    if docker_result.get("gpu_verified"):
        print("[AGENT] GPU rendering verified in Docker.")
    else:
        print("[AGENT] WARNING: GPU not verified in Docker. Renders may use CPU only.")
    print()

    # Step 3: Ensure render image is loaded
    print("[AGENT] Checking render image...")
    if not ensure_docker_image():
        print("[AGENT] WARNING: No render image available. Will retry when jobs arrive.")
    print()

    # Step 4: Detect hardware specs
    print("[AGENT] Detecting hardware specs...")
    specs = detect_specs()
    print(f"  GPU:    {specs['gpu_model']} ({specs['gpu_vram_gb']} GB VRAM)")
    print(f"  CPU:    {specs['cpu_cores']} cores")
    print(f"  RAM:    {specs['ram_gb']} GB")
    print(f"  OS:     {specs['os_version']}")
    print(f"  Driver: {specs['nvidia_driver']}")
    print()

    # Step 5: Register with backend (or reconnect with saved ID)
    saved_id = load_machine_id()

    print("[AGENT] Registering with backend...")
    try:
        machine_id = register_machine(specs)
        save_machine_id(machine_id)
        if saved_id and saved_id == machine_id:
            print(f"[AGENT] Reconnected. Machine ID: {machine_id}")
        else:
            print(f"[AGENT] Registered. Machine ID: {machine_id}")
    except Exception as e:
        print(f"[AGENT] Failed to register: {e}")
        if saved_id:
            print(f"[AGENT] Using saved machine ID: {saved_id}")
            machine_id = saved_id
        else:
            sys.exit(1)

    # Step 6: Mark available and start polling
    set_available(machine_id)
    print("[AGENT] Marked as available. Polling for jobs...\n")

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
                    print("[AGENT] Docker is not running. Waiting...")
                    time.sleep(POLL_INTERVAL * 2)
                    continue

                job = poll_for_job(machine_id)
                if job:
                    print(f"[AGENT] Got job: {job['id']} ({job['input_filename']})")

                    # Ensure image is loaded before running
                    if not check_image_loaded():
                        print("[AGENT] Render image not loaded, downloading...")
                        if not ensure_docker_image():
                            print("[AGENT] Cannot load render image, failing job.")
                            update_job_status(job["id"], "failed", error="Render image not available")
                            continue

                    execute_job(job)

                    # Re-mark available after job only if still accepting work
                    if should_offer_capacity():
                        set_available(machine_id)
                        print("[AGENT] Back to polling...\n")
                    elif pause_event.is_set() and not shutdown_event.is_set():
                        print("[AGENT] Agent is paused.\n")
                else:
                    time.sleep(POLL_INTERVAL)

            except requests.exceptions.ConnectionError:
                print(f"[AGENT] Cannot reach backend, retrying in {POLL_INTERVAL}s...")
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                print(f"[AGENT] Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)
    finally:
        if machine_id:
            set_idle(machine_id)
        print("[AGENT] Shutdown complete.")


if __name__ == "__main__":
    main()
