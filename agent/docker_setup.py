"""
PC Rent Agent - Docker Bootstrap
Handles first-time installation of Docker Desktop on Windows.
WSL2 installation is delegated entirely to the Docker Desktop installer.
"""

import ctypes
import os
import subprocess
import tempfile
import time
import urllib.request

from config import (
    load_gpu_check_cache,
    save_gpu_check_cache,
    clear_gpu_check_cache,
    load_setup_state,
    save_setup_state,
    clear_setup_state,
)

DOCKER_DESKTOP_URL = (
    "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
)
GPU_CHECK_SUCCESS_CACHE_TTL = 24 * 60 * 60
GPU_CHECK_FAILURE_CACHE_TTL = 10 * 60

# Standard Windows exit codes indicating a reboot is required
_REBOOT_EXIT_CODES = {3010, 1641}


def check_docker_installed():
    """
    Check if Docker is installed.
    First tries the CLI (fast path), then falls back to checking known
    Docker Desktop install paths — handles the case where Docker was just
    installed and PATH is not yet updated (pre-reboot).
    """
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Filesystem fallback — Docker Desktop install location on Windows
    candidates = [
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "Docker", "Docker", "Docker Desktop.exe",
        ),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "Docker", "Docker", "Docker Desktop.exe",
        ),
    ]
    return any(os.path.exists(p) for p in candidates)


def check_docker_running():
    """Check if Docker daemon is running and responsive."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def download_docker_desktop(on_status=None, on_progress=None):
    """Download Docker Desktop installer to temp directory.

    Args:
        on_status: Optional callback(message: str) for status messages.
        on_progress: Optional callback(downloaded_bytes, total_bytes, pct)
                     called during the download for real-time progress.

    Returns path on success, or None on failure.
    """
    dest = os.path.join(tempfile.gettempdir(), "DockerDesktopInstaller.exe")
    if os.path.exists(dest) and os.path.getsize(dest) > 100_000_000:
        if on_status:
            on_status("[SETUP] Docker Desktop installer already downloaded.")
        return dest

    if on_status:
        on_status("[SETUP] Downloading Docker Desktop (~500MB)...")
    try:
        resp = urllib.request.urlopen(DOCKER_DESKTOP_URL)
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 64 * 1024  # 64 KB
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress and total > 0:
                    pct = min(100.0, (downloaded / total) * 100)
                    on_progress(downloaded, total, pct)
        if on_status:
            on_status("[SETUP] Download complete.")
        return dest
    except Exception as e:
        if on_status:
            on_status(f"[SETUP] Download failed: {e}")
        # Clean up partial download
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        return None


def install_docker_desktop(on_status=None):
    """
    Install Docker Desktop using ShellExecute with the 'runas' verb so the UAC
    dialog appears in the foreground. The Docker installer handles WSL2 setup
    automatically via --backend=wsl-2.

    Polls for Docker's presence after launching (ShellExecute is async/fire-and-forget).

    Returns dict: {success, needs_reboot, message}
    """
    if on_status:
        on_status("[SETUP] Installing Docker Desktop — Windows will ask for permission to install...")

    # Expect the installer to already be downloaded by the caller.
    installer_path = os.path.join(tempfile.gettempdir(), "DockerDesktopInstaller.exe")
    if not os.path.exists(installer_path) or os.path.getsize(installer_path) < 100_000_000:
        return {
            "success": False,
            "needs_reboot": False,
            "message": "Docker Desktop installer not found. Download it first.",
        }

    try:
        # ShellExecute with 'runas' verb — brings UAC dialog to the foreground
        # rather than having it appear silently behind the app window.
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,                                        # hwnd
            "runas",                                     # verb
            installer_path,                              # file
            "install --quiet --accept-license --backend=wsl-2",  # params
            None,                                        # working dir
            1,                                           # SW_SHOWNORMAL
        )
        # ShellExecute returns an HINSTANCE value > 32 on success
        if ret <= 32:
            # Common error codes
            if ret == 5:
                msg = "Docker installation was cancelled (UAC was declined). Click Refresh to try again."
            elif ret == 2:
                msg = "Docker installer file not found. Click Refresh to re-download."
            else:
                msg = (
                    f"Could not launch the Docker installer (error code {ret}). "
                    "Try running the app as administrator."
                )
            return {"success": False, "needs_reboot": False, "message": msg}
    except Exception as e:
        return {
            "success": False,
            "needs_reboot": False,
            "message": f"Failed to launch Docker installer: {e}",
        }

    # Poll until Docker Desktop appears on disk (up to 10 minutes).
    # We cannot waitpid on an elevated child process launched via ShellExecute.
    deadline = time.time() + 600
    poll_interval = 5
    elapsed = 0
    while time.time() < deadline:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if check_docker_installed():
            if on_status:
                on_status("[SETUP] Docker Desktop installed successfully.")
            return {"success": True, "needs_reboot": False, "message": "Docker installed."}
        if on_status and elapsed % 15 == 0:
            on_status(f"[SETUP] Installing Docker Desktop... ({elapsed}s)")

    return {
        "success": False,
        "needs_reboot": False,
        "message": (
            "Docker installation timed out (10 min). "
            "Please install Docker Desktop manually from https://www.docker.com/products/docker-desktop/"
        ),
    }


def _try_start_docker_desktop(on_status=None):
    """Attempt to launch Docker Desktop if it's installed but not running."""
    docker_exe = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        "Docker", "Docker", "Docker Desktop.exe",
    )
    if os.path.exists(docker_exe):
        if on_status:
            on_status("[SETUP] Starting Docker Desktop...")
        try:
            subprocess.Popen([docker_exe])
        except Exception:
            pass


def wait_for_docker_ready(timeout=120, on_status=None):
    """
    Wait for Docker daemon to become responsive.
    Returns True when ready, False on timeout.
    """
    if on_status:
        on_status("[SETUP] Waiting for Docker to start...")
    waited = 0
    interval = 5
    while waited < timeout:
        if check_docker_running():
            if on_status:
                on_status("[SETUP] Docker is ready.")
            return True
        time.sleep(interval)
        waited += interval
        if on_status and waited % 20 == 0:
            on_status(f"[SETUP] Still waiting for Docker... ({waited}s)")

    if on_status:
        on_status(f"[SETUP] Docker not ready after {timeout}s.")
    return False


def get_cached_gpu_verification():
    """Return a recent cached Docker GPU verification result, if available."""
    cache = load_gpu_check_cache()
    if not cache:
        return None

    verified = cache.get("gpu_verified")
    checked_at = cache.get("checked_at")
    if not isinstance(verified, bool) or not isinstance(checked_at, (int, float)):
        return None

    ttl = GPU_CHECK_SUCCESS_CACHE_TTL if verified else GPU_CHECK_FAILURE_CACHE_TTL
    age = time.time() - checked_at
    if age < 0 or age > ttl:
        return None

    gpu_name = cache.get("gpu_docker_name", "")
    if not isinstance(gpu_name, str):
        gpu_name = ""
    gpu_error = cache.get("gpu_error", "")
    if not isinstance(gpu_error, str):
        gpu_error = ""
    # Legacy cache entries (before gpu_error existed) can preserve
    # stale false negatives. Force a fresh probe in that case.
    if not verified and not gpu_error:
        return None

    return {
        "gpu_verified": verified,
        "gpu_docker_name": gpu_name,
        "gpu_error": gpu_error,
        "checked_at": checked_at,
    }


def _save_gpu_verification(gpu_name, gpu_error=""):
    save_gpu_check_cache({
        "gpu_verified": bool(gpu_name),
        "gpu_docker_name": gpu_name,
        "gpu_error": gpu_error,
        "checked_at": time.time(),
    })


def resolve_gpu_verification(use_cache=True, on_status=None):
    """Return a Docker GPU verification result, reusing a recent cache when allowed."""
    if use_cache:
        cached = get_cached_gpu_verification()
        if cached is not None:
            print("[SETUP] Using cached Docker GPU verification result.")
            return cached

    probe = verify_gpu_in_docker(on_status=on_status)
    gpu_name = probe.get("gpu_name", "")
    gpu_error = probe.get("error", "")
    return {
        "gpu_verified": bool(gpu_name),
        "gpu_docker_name": gpu_name,
        "gpu_error": gpu_error,
        "checked_at": time.time(),
    }


def _check_nvidia_runtime_in_docker():
    """
    Check if Docker has the nvidia container runtime configured.
    Returns (available: bool, detail: str).
    """
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        if result.returncode != 0:
            detail = stderr or stdout or f"exit code {result.returncode}"
            return False, f"docker info failed: {detail}"
        if stderr:
            return False, f"docker info reported an error: {stderr}"
        if not stdout or stdout == "null":
            return False, f"Docker runtimes unavailable: {stdout or 'empty output'}"
        if "nvidia" in stdout.lower():
            return True, "nvidia runtime found"
        return False, f"nvidia runtime not found in Docker runtimes: {stdout}"
    except Exception as e:
        return False, f"Could not query Docker runtimes: {e}"
    return False, "docker info returned an unknown error"


def verify_gpu_in_docker(on_status=None):
    """
    Run a quick GPU test inside Docker to confirm GPU passthrough works.
    Returns dict:
        gpu_name: GPU name string if accessible, else ""
        error: failure detail string when unavailable

    Pulls the test image first if needed, then retries the GPU check once.
    Surfaces real Docker error messages via on_status.
    """
    def status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    image = "nvidia/cuda:12.2.0-base-ubuntu22.04"

    # First check if nvidia runtime is registered in Docker
    runtime_ok, runtime_detail = _check_nvidia_runtime_in_docker()
    precheck_warning = ""
    if not runtime_ok:
        precheck_warning = runtime_detail
        status(
            f"[SETUP] Docker GPU pre-check warning: {runtime_detail}. "
            "Continuing with direct GPU probe..."
        )

    # Ensure the test image is available locally
    try:
        check = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if check.returncode != 0:
            status(f"[SETUP] Pulling GPU test image ({image})...")
            subprocess.run(
                ["docker", "pull", image],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
    except Exception:
        pass

    # Try the GPU test (retry once on failure)
    last_error = ""
    for attempt in range(2):
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--gpus", "all",
                    image,
                    "nvidia-smi",
                    "--query-gpu=name",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                gpu = result.stdout.strip().split("\n")[0]
                _save_gpu_verification(gpu, "")
                status(f"[SETUP] GPU verified in Docker: {gpu}")
                return {"gpu_name": gpu, "error": ""}
            else:
                last_error = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                status(f"[SETUP] GPU test attempt {attempt + 1} failed: {last_error}")
        except Exception as e:
            last_error = str(e)
            status(f"[SETUP] GPU test attempt {attempt + 1} error: {last_error}")

        if attempt == 0:
            time.sleep(2)

    if last_error:
        combined_error = (
            f"{last_error} (runtime pre-check: {precheck_warning})"
            if precheck_warning
            else last_error
        )
        status(
            f"[SETUP] GPU not accessible in Docker. Error: {combined_error}. "
            "Check Docker Desktop GPU support and restart Docker Desktop."
        )
    else:
        combined_error = precheck_warning or "Unknown GPU verification failure."

    _save_gpu_verification("", combined_error)
    return {"gpu_name": "", "error": combined_error}


def full_bootstrap(on_status=None, force_gpu_recheck=False):
    """
    Run the full Docker bootstrap flow. WSL2 is handled by Docker's own installer.

    Args:
        on_status: Optional callback(message: str) for status updates.

    Returns:
        dict with:
            ready: bool
            needs_reboot: bool
            message: str
            gpu_verified: bool  (only when ready=True)
            gpu_docker_name: str (only when ready=True)
    """

    def status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    if force_gpu_recheck:
        clear_gpu_check_cache()

    # Fast path: Docker already installed and running
    if check_docker_installed() and check_docker_running():
        status("[SETUP] Docker is already installed and running.")
        clear_setup_state()
        gpu_result = resolve_gpu_verification(use_cache=not force_gpu_recheck, on_status=on_status)
        return {
            "ready": True,
            "needs_reboot": False,
            "gpu_verified": gpu_result["gpu_verified"],
            "gpu_docker_name": gpu_result["gpu_docker_name"],
            "gpu_error": gpu_result.get("gpu_error", ""),
            "message": "Docker is ready.",
        }

    # Install Docker Desktop if not present
    if not check_docker_installed():
        install_result = install_docker_desktop(on_status=on_status)
        if not install_result["success"]:
            return {
                "ready": False,
                "needs_reboot": False,
                "message": install_result["message"],
            }

    # Docker is now installed — try to start it and wait for the daemon
    if not check_docker_running():
        _try_start_docker_desktop(on_status=on_status)
        if not wait_for_docker_ready(timeout=120, on_status=on_status):
            # Docker installed but daemon won't start — likely needs a reboot
            # to activate the WSL2 kernel that Docker's installer enabled.
            return {
                "ready": False,
                "needs_reboot": True,
                "message": (
                    "Docker is installed but needs a restart to finish setup. "
                    "Please restart your PC, then open the app again."
                ),
            }

    # Verify GPU passthrough
    status("[SETUP] Verifying GPU access in Docker containers...")
    gpu_result = resolve_gpu_verification()

    if gpu_result["gpu_verified"]:
        status(f"[SETUP] GPU verified in Docker: {gpu_result['gpu_docker_name']} - ready for rendering.")
    else:
        detail = gpu_result.get("gpu_error", "GPU verification failed.")
        status(f"[SETUP] WARNING: GPU not accessible in Docker. {detail}")

    clear_setup_state()
    return {
        "ready": True,
        "needs_reboot": False,
        "gpu_verified": gpu_result["gpu_verified"],
        "gpu_docker_name": gpu_result["gpu_docker_name"],
        "gpu_error": gpu_result.get("gpu_error", ""),
        "message": "Docker is ready.",
    }


if __name__ == "__main__":
    result = full_bootstrap()
    print()
    print(f"Result: {result}")
