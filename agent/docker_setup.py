"""
PC Rent Agent - Docker & WSL2 Bootstrap
Handles first-time installation of WSL2 and Docker Desktop.
"""

import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from config import load_setup_state, save_setup_state, clear_setup_state

DOCKER_DESKTOP_URL = (
    "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
)


def check_docker_installed():
    """Check if Docker CLI is available."""
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_docker_running():
    """Check if Docker daemon is running and responsive."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_wsl2_installed():
    """Check if WSL2 is installed and ready."""
    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def install_wsl2():
    """
    Install WSL2 (requires admin/UAC).
    Returns True if installed (may need reboot), False if failed.
    """
    print("[SETUP] Installing WSL2...")
    try:
        result = subprocess.run(
            ["wsl", "--install", "--no-distribution"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            print("[SETUP] WSL2 installed successfully.")
            return True
        else:
            print(f"[SETUP] WSL2 install returned code {result.returncode}")
            print(f"  stdout: {result.stdout}")
            print(f"  stderr: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("[SETUP] WSL2 install timed out.")
        return False
    except FileNotFoundError:
        print("[SETUP] 'wsl' command not found.")
        return False


def needs_reboot():
    """
    Check if a reboot is needed after WSL2 install.
    WSL2 needs a reboot if it was just installed and wsl --status fails.
    """
    state = load_setup_state()
    if state.get("wsl2_installed") and not check_wsl2_installed():
        return True
    return False


def schedule_resume_after_reboot():
    """
    Add agent to Windows RunOnce registry key so it auto-resumes after reboot.
    """
    try:
        import winreg

        exe_path = sys.executable
        # If running as PyInstaller bundle, use the exe path
        if getattr(sys, "frozen", False):
            exe_path = sys.executable

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "PCRentAgentSetup", 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(key)
        print("[SETUP] Registered for auto-resume after reboot.")
        return True
    except Exception as e:
        print(f"[SETUP] Failed to set RunOnce key: {e}")
        return False


def download_docker_desktop():
    """Download Docker Desktop installer to temp directory."""
    dest = os.path.join(tempfile.gettempdir(), "DockerDesktopInstaller.exe")
    if os.path.exists(dest) and os.path.getsize(dest) > 100_000_000:
        print("[SETUP] Docker Desktop installer already downloaded.")
        return dest

    print("[SETUP] Downloading Docker Desktop (~500MB)...")
    try:
        urllib.request.urlretrieve(DOCKER_DESKTOP_URL, dest)
        print("[SETUP] Download complete.")
        return dest
    except Exception as e:
        print(f"[SETUP] Download failed: {e}")
        return None


def install_docker_desktop():
    """
    Install Docker Desktop silently (requires admin/UAC).
    Returns True if install succeeded.
    """
    installer_path = download_docker_desktop()
    if not installer_path:
        return False

    print("[SETUP] Installing Docker Desktop (this may take a few minutes)...")
    try:
        result = subprocess.run(
            [installer_path, "install", "--quiet", "--accept-license"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            print("[SETUP] Docker Desktop installed successfully.")
            return True
        else:
            print(f"[SETUP] Docker install returned code {result.returncode}")
            print(f"  stderr: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("[SETUP] Docker install timed out (10 min).")
        return False


def wait_for_docker_ready(timeout=120):
    """
    Wait for Docker daemon to be ready.
    Returns True when ready, False on timeout.
    """
    print("[SETUP] Waiting for Docker daemon to start...")
    waited = 0
    interval = 5
    while waited < timeout:
        if check_docker_running():
            print("[SETUP] Docker daemon is ready.")
            return True
        time.sleep(interval)
        waited += interval
        print(f"[SETUP] Still waiting... ({waited}s)")

    print(f"[SETUP] Docker not ready after {timeout}s.")
    return False


def verify_gpu_in_docker():
    """
    Run a quick GPU test inside Docker to confirm GPU passthrough works.
    Returns True if GPU is accessible inside containers.
    """
    print("[SETUP] Verifying GPU access in Docker...")
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--gpus",
                "all",
                "nvidia/cuda:12.2.0-base-ubuntu22.04",
                "nvidia-smi",
                "--query-gpu=name",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu = result.stdout.strip().split("\n")[0]
            print(f"[SETUP] GPU verified in Docker: {gpu}")
            return True
        else:
            print(f"[SETUP] GPU test failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"[SETUP] GPU verification error: {e}")
        return False


def full_bootstrap(on_status=None):
    """
    Run the full Docker bootstrap flow.

    Args:
        on_status: Optional callback(message: str) for status updates.

    Returns:
        dict with:
            ready: bool
            needs_reboot: bool
            message: str
    """

    def status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    state = load_setup_state()

    # Step 1: Check if Docker is already installed and running
    if check_docker_installed() and check_docker_running():
        status("[SETUP] Docker is already installed and running.")
        clear_setup_state()
        return {"ready": True, "needs_reboot": False, "message": "Docker ready"}

    # Step 2: Check/install WSL2
    if not check_wsl2_installed():
        if state.get("wsl2_installed"):
            # WSL2 was installed last run but needs reboot
            status("[SETUP] WSL2 was installed. A reboot is required.")
            schedule_resume_after_reboot()
            save_setup_state({"wsl2_installed": True, "stage": "needs_reboot"})
            return {
                "ready": False,
                "needs_reboot": True,
                "message": "Please restart your PC to complete WSL2 setup, then run the agent again.",
            }

        status("[SETUP] WSL2 not found. Installing...")
        if install_wsl2():
            save_setup_state({"wsl2_installed": True, "stage": "wsl2_done"})
            # Check if reboot is needed
            if not check_wsl2_installed():
                status("[SETUP] Reboot required to activate WSL2.")
                schedule_resume_after_reboot()
                save_setup_state({"wsl2_installed": True, "stage": "needs_reboot"})
                return {
                    "ready": False,
                    "needs_reboot": True,
                    "message": "Please restart your PC to complete WSL2 setup, then run the agent again.",
                }
        else:
            return {
                "ready": False,
                "needs_reboot": False,
                "message": "Failed to install WSL2. Please run as administrator.",
            }

    # Step 3: Install Docker Desktop if not present
    if not check_docker_installed():
        status("[SETUP] Docker not found. Installing Docker Desktop...")
        if not install_docker_desktop():
            return {
                "ready": False,
                "needs_reboot": False,
                "message": "Failed to install Docker Desktop.",
            }

    # Step 4: Wait for Docker to be ready
    if not check_docker_running():
        # Try to start Docker Desktop
        status("[SETUP] Starting Docker Desktop...")
        try:
            docker_path = os.path.join(
                os.environ.get("ProgramFiles", "C:\\Program Files"),
                "Docker",
                "Docker",
                "Docker Desktop.exe",
            )
            if os.path.exists(docker_path):
                subprocess.Popen([docker_path])
        except Exception:
            pass

        if not wait_for_docker_ready(timeout=120):
            return {
                "ready": False,
                "needs_reboot": False,
                "message": "Docker daemon failed to start. Please start Docker Desktop manually.",
            }

    # Step 5: Verify GPU passthrough
    status("[SETUP] Verifying GPU access in Docker containers...")
    gpu_ok = verify_gpu_in_docker()
    if not gpu_ok:
        status("[SETUP] WARNING: GPU not accessible in Docker. Rendering will use CPU.")

    clear_setup_state()
    return {
        "ready": True,
        "needs_reboot": False,
        "gpu_verified": gpu_ok,
        "message": "Docker is ready." + (" GPU verified." if gpu_ok else " GPU NOT available."),
    }


if __name__ == "__main__":
    result = full_bootstrap()
    print()
    print(f"Result: {result}")
