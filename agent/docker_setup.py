"""
PC Rent Agent - Docker & WSL2 Bootstrap
Handles first-time installation of WSL2 and Docker Desktop.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from config import (
    load_gpu_check_cache,
    save_gpu_check_cache,
    clear_setup_state,
)

DOCKER_DESKTOP_URL = (
    "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
)
GPU_CHECK_SUCCESS_CACHE_TTL = 24 * 60 * 60
GPU_CHECK_FAILURE_CACHE_TTL = 10 * 60
_DOCKER_CLI_CACHE = None


def _append_to_process_path(path_dir):
    if not path_dir:
        return
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    normalized = {p.lower() for p in parts}
    if path_dir.lower() in normalized:
        return
    os.environ["PATH"] = f"{path_dir}{os.pathsep}{current}" if current else path_dir


def resolve_docker_cli(refresh=False):
    """
    Resolve a usable Docker CLI executable path.
    Returns absolute executable path or None.
    """
    global _DOCKER_CLI_CACHE

    if not refresh and _DOCKER_CLI_CACHE and os.path.exists(_DOCKER_CLI_CACHE):
        return _DOCKER_CLI_CACHE

    candidates = []
    which_path = shutil.which("docker")
    if which_path:
        candidates.append(which_path)

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.extend([
        os.path.join(program_files, "Docker", "Docker", "resources", "bin", "docker.exe"),
        os.path.join(program_files, "Docker", "Docker", "resources", "docker-cli.exe"),
        os.path.join(program_files, "Docker", "Docker", "resources", "docker.exe"),
    ])

    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue
        if result.returncode == 0:
            _DOCKER_CLI_CACHE = path
            _append_to_process_path(os.path.dirname(path))
            return path

    _DOCKER_CLI_CACHE = None
    return None


def run_docker_cli(args, timeout=30, capture_output=True, text=True):
    docker_cli = resolve_docker_cli()
    if not docker_cli:
        raise FileNotFoundError("Docker CLI not found")
    return subprocess.run(
        [docker_cli, *args],
        capture_output=capture_output,
        text=text,
        timeout=timeout,
    )


def schedule_auto_reboot(delay_seconds=20):
    """
    Schedule a Windows reboot after a short delay.
    Returns True if scheduling succeeded.
    """
    try:
        result = subprocess.run(
            [
                "shutdown",
                "/r",
                "/t",
                str(delay_seconds),
                "/c",
                "PC Rent installed WSL2 and needs restart to continue setup.",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print(f"[SETUP] Restart scheduled in {delay_seconds} seconds.")
            return True
        print(f"[SETUP] Failed to schedule restart (code {result.returncode}).")
        if result.stderr:
            print(f"  stderr: {result.stderr}")
        return False
    except Exception as e:
        print(f"[SETUP] Failed to schedule restart: {e}")
        return False


def _run_elevated_process(file_path, arguments, timeout):
    """
    Launch a process via UAC elevation prompt and wait for completion.
    Returns subprocess.CompletedProcess from the PowerShell wrapper.
    """
    escaped_path = file_path.replace("'", "''")
    quoted_args = []
    for arg in arguments:
        quoted_args.append("'" + str(arg).replace("'", "''") + "'")
    ps_args = ", ".join(quoted_args)
    arg_expr = f"@({ps_args})" if ps_args else "@()"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$proc = Start-Process -FilePath '{escaped_path}' "
        f"-ArgumentList {arg_expr} -Verb RunAs -PassThru -Wait; "
        "exit $proc.ExitCode"
    )
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def check_docker_installed():
    """Check if Docker CLI is available."""
    return resolve_docker_cli(refresh=True) is not None


def check_docker_running():
    """Check if Docker daemon is running and responsive."""
    try:
        result = run_docker_cli(["info"], timeout=15)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_windows_feature_state(feature_name):
    """
    Return normalized DISM feature state:
    enabled | enable pending | disabled | disable pending | unknown
    """
    try:
        result = subprocess.run(
            [
                "dism",
                "/online",
                "/get-featureinfo",
                f"/featurename:{feature_name}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return "unknown"

        for raw_line in (result.stdout or "").splitlines():
            line = raw_line.strip()
            if line.lower().startswith("state :"):
                return line.split(":", 1)[1].strip().lower()
    except Exception:
        pass

    return "unknown"


def _feature_state_is_disabled(state):
    normalized = (state or "").strip().lower()
    return normalized.startswith("disabled") or normalized.startswith("disable pending")


def check_wsl2_installed():
    """Check if WSL2 is installed and ready."""
    # Guard against explicit disabled states, but do not fail on unknown query results.
    wsl_feature_state = get_windows_feature_state("Microsoft-Windows-Subsystem-Linux")
    vm_feature_state = get_windows_feature_state("VirtualMachinePlatform")
    if _feature_state_is_disabled(wsl_feature_state):
        return False
    if _feature_state_is_disabled(vm_feature_state):
        return False

    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False

        output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        blocked_markers = (
            "wsl optional component is not enabled",
            "wsl_e_wsl_optional_component_required",
            "virtual machine platform",
            "kernel component",
        )
        if any(marker in output for marker in blocked_markers):
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _enable_windows_feature(feature_name):
    """
    Enable a Windows optional feature via DISM.
    Returns (ok, detail_message).
    """
    args = [
        "/online",
        "/enable-feature",
        f"/featurename:{feature_name}",
        "/all",
        "/norestart",
    ]
    try:
        result = subprocess.run(
            ["dism", *args],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode in (0, 3010):
            return True, ""
    except Exception:
        result = None

    print(f"[SETUP] Requesting administrator permission to enable {feature_name}...")
    try:
        elevated = _run_elevated_process("dism", args, timeout=600)
    except Exception as exc:
        return False, str(exc)

    if elevated.returncode in (0, 3010):
        return True, ""

    detail = _compact_error_text(
        getattr(elevated, "stderr", ""),
        getattr(elevated, "stdout", ""),
        getattr(result, "stderr", "") if result else "",
    )
    if not detail:
        detail = f"DISM failed with code {elevated.returncode}."
    return False, detail


def ensure_wsl_windows_features_enabled():
    """
    Ensure WSL-required Windows features are enabled.
    Returns (ok, message).
    """
    required = [
        "Microsoft-Windows-Subsystem-Linux",
        "VirtualMachinePlatform",
    ]
    for feature_name in required:
        state = get_windows_feature_state(feature_name)
        if state == "enabled":
            continue

        print(f"[SETUP] Enabling Windows feature: {feature_name}...")
        ok, detail = _enable_windows_feature(feature_name)
        if not ok:
            return False, f"Failed to enable Windows feature {feature_name}: {detail}"

    return True, ""


def install_wsl2():
    """
    Install WSL2 (requires admin/UAC).
    Returns True if installed (may need reboot), False if failed.
    """
    print("[SETUP] Installing WSL2...")
    features_ok, features_error = ensure_wsl_windows_features_enabled()
    if not features_ok:
        print(f"[SETUP] {features_error}")
        return False

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
        print(f"[SETUP] WSL2 install returned code {result.returncode}")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        print("[SETUP] Requesting administrator permission for WSL2 install...")
        elevated = _run_elevated_process(
            "wsl",
            ["--install", "--no-distribution"],
            timeout=600,
        )
        if elevated.returncode == 0:
            print("[SETUP] WSL2 installed successfully (elevated).")
            return True
        print(f"[SETUP] Elevated WSL2 install failed with code {elevated.returncode}")
        print(f"  stdout: {elevated.stdout}")
        print(f"  stderr: {elevated.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("[SETUP] WSL2 install timed out.")
        return False
    except FileNotFoundError:
        print("[SETUP] 'wsl' command not found.")
        return False


def ensure_wsl2_ready(on_status=None):
    """
    Ensure WSL2 prerequisites are present and WSL is usable.
    Returns:
        dict with ready, needs_reboot, message
    """
    def status(msg):
        print(msg)
        if on_status:
            on_status(msg)

    if check_wsl2_installed():
        status("[SETUP] WSL2 is ready.")
        return {
            "ready": True,
            "needs_reboot": False,
            "message": "WSL2 is ready.",
        }

    status("[SETUP] WSL2 not found. Installing...")
    if not install_wsl2():
        return {
            "ready": False,
            "needs_reboot": False,
            "message": "Failed to install WSL2. Approve the Windows admin (UAC) prompt or run app as administrator.",
        }

    if check_wsl2_installed():
        status("[SETUP] WSL2 is ready.")
        return {
            "ready": True,
            "needs_reboot": False,
            "message": "WSL2 is ready.",
        }

    wsl_feature_state = get_windows_feature_state("Microsoft-Windows-Subsystem-Linux")
    vm_feature_state = get_windows_feature_state("VirtualMachinePlatform")

    if "enable pending" in {wsl_feature_state, vm_feature_state}:
        # Reboot is only valid when Windows explicitly reports pending enable.
        status("[SETUP] WSL2 installed. Reboot required before Docker setup.")
        schedule_resume_after_reboot()
        reboot_scheduled = schedule_auto_reboot(delay_seconds=20)
        if reboot_scheduled:
            message = "WSL2 installed. Restarting your PC in 20 seconds to continue setup."
        else:
            message = "WSL2 installed. Please restart your PC to continue setup."
        return {
            "ready": False,
            "needs_reboot": True,
            "message": message,
        }

    if (
        wsl_feature_state != "enabled"
        or vm_feature_state != "enabled"
    ):
        return {
            "ready": False,
            "needs_reboot": False,
            "message": (
                "WSL setup did not enable required Windows features. "
                "Run setup as administrator and ensure policy allows enabling "
                "'Windows Subsystem for Linux' and 'Virtual Machine Platform'."
            ),
        }

    return {
        "ready": False,
        "needs_reboot": False,
        "message": (
            "WSL features are enabled but WSL2 is still unavailable. "
            "Enable CPU virtualization in BIOS/UEFI and retry."
        ),
    }


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


def _compact_error_text(*values):
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        line = text.splitlines()[0].strip()
        if line:
            return line
    return ""


def find_winget_executable():
    candidates = []

    which_path = shutil.which("winget")
    if which_path:
        candidates.append(which_path)

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(
            os.path.join(local_app, "Microsoft", "WindowsApps", "winget.exe")
        )

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def install_docker_via_winget():
    """
    Install Docker Desktop using winget as a fallback path.
    Returns (ok, reason). reason is empty on success.
    """
    winget_exe = find_winget_executable()
    if not winget_exe:
        return False, "winget is not available on this system."

    print("[SETUP] Attempting Docker Desktop install via winget fallback...")
    args = [
        "install",
        "-e",
        "--id",
        "Docker.DockerDesktop",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    try:
        result = _run_elevated_process(winget_exe, args, timeout=1800)
    except subprocess.TimeoutExpired:
        return False, "winget Docker install timed out."
    except Exception as exc:
        return False, f"winget Docker install failed to launch: {exc}"

    if result.returncode == 0:
        print("[SETUP] Docker Desktop installed successfully via winget.")
        return True, ""

    text = _compact_error_text(result.stderr, result.stdout)
    blob = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    if (
        result.returncode == 1223
        or "operation was canceled" in blob
        or "canceled by the user" in blob
    ):
        return False, "Docker install via winget was canceled at the Windows admin (UAC) prompt."

    if text:
        return False, f"winget Docker install failed (code {result.returncode}): {text}"
    return False, f"winget Docker install failed (code {result.returncode})."


def _download_docker_via_powershell(url, dest):
    escaped_url = url.replace("'", "''")
    escaped_dest = dest.replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
        f"Invoke-WebRequest -Uri '{escaped_url}' -OutFile '{escaped_dest}'"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=1200,
        )
    except Exception as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, ""

    detail = _compact_error_text(result.stderr, result.stdout)
    if detail:
        return False, detail
    return False, f"PowerShell downloader failed (code {result.returncode})."


def _download_docker_via_bits(url, dest):
    escaped_url = url.replace("'", "''")
    escaped_dest = dest.replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Start-BitsTransfer -Source '{escaped_url}' -Destination '{escaped_dest}'"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except Exception as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, ""

    detail = _compact_error_text(result.stderr, result.stdout)
    if detail:
        return False, detail
    return False, f"BITS downloader failed (code {result.returncode})."


def download_docker_desktop():
    """Download Docker Desktop installer to temp directory.
    Returns (path_or_none, error_message)."""
    dest = os.path.join(tempfile.gettempdir(), "DockerDesktopInstaller.exe")
    if os.path.exists(dest):
        if os.path.getsize(dest) > 100_000_000:
            print("[SETUP] Docker Desktop installer already downloaded.")
            return dest, ""
        try:
            os.remove(dest)
        except Exception:
            pass

    print("[SETUP] Downloading Docker Desktop (~500MB)...")

    try:
        urllib.request.urlretrieve(DOCKER_DESKTOP_URL, dest)
        if os.path.exists(dest) and os.path.getsize(dest) > 100_000_000:
            print("[SETUP] Download complete.")
            return dest, ""
        raise RuntimeError("Downloaded installer is incomplete.")
    except Exception as first_err:
        print(f"[SETUP] Primary download failed: {first_err}")

    ok, ps_error = _download_docker_via_powershell(DOCKER_DESKTOP_URL, dest)
    if ok and os.path.exists(dest) and os.path.getsize(dest) > 100_000_000:
        print("[SETUP] Download complete (PowerShell fallback).")
        return dest, ""

    bits_ok, bits_error = _download_docker_via_bits(DOCKER_DESKTOP_URL, dest)
    if bits_ok and os.path.exists(dest) and os.path.getsize(dest) > 100_000_000:
        print("[SETUP] Download complete (BITS fallback).")
        return dest, ""

    error_text = _compact_error_text(str(ps_error), str(bits_error))
    if not error_text:
        error_text = "Unknown network/download error."
    print(f"[SETUP] Fallback download failed: {error_text}")
    return None, error_text


def install_docker_desktop():
    """
    Install Docker Desktop silently (requires admin/UAC).
    Returns (ok, reason). reason is empty on success.
    """
    installer_path, download_error = download_docker_desktop()
    if not installer_path:
        if download_error:
            print(f"[SETUP] Docker installer download unavailable: {download_error}")
        else:
            print("[SETUP] Docker installer download unavailable.")

        winget_ok, winget_error = install_docker_via_winget()
        if winget_ok:
            return True, ""

        if download_error and winget_error:
            return False, (
                f"Docker installer download failed: {download_error}. "
                f"winget fallback failed: {winget_error}"
            )
        if winget_error:
            return False, f"Docker installer download failed. winget fallback failed: {winget_error}"
        if download_error:
            return False, f"Docker installer download failed: {download_error}"
        return False, "Docker installer download failed."
    if not os.path.exists(installer_path):
        return False, f"Docker installer not found: {installer_path}"

    print("[SETUP] Installing Docker Desktop (this may take a few minutes)...")
    install_args = ["install", "--quiet", "--accept-license"]
    direct_result = None
    elevated_result = None

    try:
        direct_result = subprocess.run(
            [installer_path, *install_args],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if direct_result.returncode == 0:
            print("[SETUP] Docker Desktop installer finished (direct).")
        else:
            print(f"[SETUP] Docker install returned code {direct_result.returncode}")
            print(f"  stderr: {direct_result.stderr}")
    except subprocess.TimeoutExpired:
        print("[SETUP] Docker install timed out (direct).")
    except Exception as e:
        print(f"[SETUP] Docker install failed to launch (direct): {e}")

    if not check_docker_installed():
        print("[SETUP] Requesting administrator permission for Docker install...")
        try:
            elevated_result = _run_elevated_process(
                installer_path,
                install_args,
                timeout=900,
            )
            if elevated_result.returncode == 0:
                print("[SETUP] Docker Desktop installer finished (elevated).")
            else:
                print(f"[SETUP] Elevated Docker install failed with code {elevated_result.returncode}")
                print(f"  stdout: {elevated_result.stdout}")
                print(f"  stderr: {elevated_result.stderr}")
        except subprocess.TimeoutExpired:
            return False, "Docker installer timed out."
        except Exception as e:
            return False, f"Docker installer launch failed: {e}"

    if check_docker_installed():
        return True, ""

    # Final fallback: run elevated without --quiet to maximize compatibility.
    print("[SETUP] Retrying Docker installer with compatibility arguments...")
    try:
        compat_result = _run_elevated_process(
            installer_path,
            ["install", "--accept-license"],
            timeout=1200,
        )
    except subprocess.TimeoutExpired:
        return False, "Docker installer timed out."
    except Exception as e:
        return False, f"Docker installer launch failed: {e}"

    if compat_result.returncode == 0 and check_docker_installed():
        print("[SETUP] Docker Desktop installed successfully (compat mode).")
        return True, ""

    combined_text = "\n".join([
        getattr(direct_result, "stderr", "") if direct_result else "",
        getattr(direct_result, "stdout", "") if direct_result else "",
        getattr(elevated_result, "stderr", "") if elevated_result else "",
        getattr(elevated_result, "stdout", "") if elevated_result else "",
        compat_result.stderr or "",
        compat_result.stdout or "",
    ]).lower()
    if (
        compat_result.returncode == 1223
        or "operation was canceled" in combined_text
        or "canceled by the user" in combined_text
    ):
        return False, "Docker install was canceled at the Windows admin (UAC) prompt."

    detail = _compact_error_text(
        compat_result.stderr,
        compat_result.stdout,
        getattr(elevated_result, "stderr", "") if elevated_result else "",
        getattr(direct_result, "stderr", "") if direct_result else "",
    )
    if detail:
        return False, f"Docker installer failed: {detail}"
    return False, f"Docker installer failed (code {compat_result.returncode})."


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


def launch_docker_desktop():
    candidates = []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates.append(os.path.join(program_files, "Docker", "Docker", "Docker Desktop.exe"))
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "Docker", "Docker", "Docker Desktop.exe"))

    for docker_path in candidates:
        if not docker_path or not os.path.exists(docker_path):
            continue
        try:
            subprocess.Popen([docker_path])
            return True
        except Exception:
            pass

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

    return {
        "gpu_verified": verified,
        "gpu_docker_name": gpu_name,
        "checked_at": checked_at,
    }


def _save_gpu_verification(gpu_name):
    save_gpu_check_cache({
        "gpu_verified": bool(gpu_name),
        "gpu_docker_name": gpu_name,
        "checked_at": time.time(),
    })


def resolve_gpu_verification(use_cache=True):
    """Return a Docker GPU verification result, reusing a recent cache when allowed."""
    if use_cache:
        cached = get_cached_gpu_verification()
        if cached is not None:
            print("[SETUP] Using cached Docker GPU verification result.")
            return cached

    gpu_name = verify_gpu_in_docker()
    return {
        "gpu_verified": bool(gpu_name),
        "gpu_docker_name": gpu_name,
        "checked_at": time.time(),
    }


def verify_gpu_in_docker():
    """
    Run a quick GPU test inside Docker to confirm GPU passthrough works.
    Returns the GPU name string if accessible, or empty string on failure.
    Pulls the test image first if needed, then retries the GPU check once.
    """
    image = "nvidia/cuda:12.2.0-base-ubuntu22.04"

    # Ensure the test image is available locally
    try:
        check = run_docker_cli(
            ["image", "inspect", image],
            timeout=10,
        )
        if check.returncode != 0:
            print(f"[SETUP] Pulling GPU test image ({image})...")
            run_docker_cli(
                ["pull", image],
                timeout=300,
            )
    except Exception:
        pass

    # Try the GPU test (retry once on failure)
    for attempt in range(2):
        try:
            result = run_docker_cli(
                [
                    "run", "--rm",
                    "--gpus", "all",
                    image,
                    "nvidia-smi",
                    "--query-gpu=name",
                    "--format=csv,noheader",
                ],
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                gpu = result.stdout.strip().split("\n")[0]
                _save_gpu_verification(gpu)
                print(f"[SETUP] GPU verified in Docker: {gpu}")
                return gpu
            else:
                print(f"[SETUP] GPU test attempt {attempt + 1} failed: {result.stderr.strip()}")
        except Exception as e:
            print(f"[SETUP] GPU test attempt {attempt + 1} error: {e}")

        if attempt == 0:
            time.sleep(2)

    _save_gpu_verification("")
    return ""


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

    # Step 1: Check if Docker is already installed and running
    if check_docker_installed() and check_docker_running():
        status("[SETUP] Docker is already installed and running.")
        clear_setup_state()
        gpu_result = resolve_gpu_verification()
        return {
            "ready": True,
            "needs_reboot": False,
            "gpu_verified": gpu_result["gpu_verified"],
            "gpu_docker_name": gpu_result["gpu_docker_name"],
            "message": "Docker is ready.",
        }

    # Step 2: Ensure WSL2 is ready
    wsl_result = ensure_wsl2_ready(on_status=on_status)
    if not wsl_result.get("ready"):
        return {
            "ready": False,
            "needs_reboot": wsl_result.get("needs_reboot", False),
            "message": wsl_result.get("message", "Failed to prepare WSL2."),
        }
    clear_setup_state()

    # Step 3: Install Docker Desktop if not present
    if not check_docker_installed():
        status("[SETUP] Docker not found. Installing Docker Desktop...")
        install_ok, install_error = install_docker_desktop()
        if not install_ok:
            return {
                "ready": False,
                "needs_reboot": False,
                "message": install_error or "Failed to install Docker Desktop.",
            }

    # Step 4: Wait for Docker to be ready
    if not check_docker_running():
        # Try to start Docker Desktop
        status("[SETUP] Starting Docker Desktop...")
        launch_docker_desktop()

        if not wait_for_docker_ready(timeout=120):
            return {
                "ready": False,
                "needs_reboot": False,
                "message": "Docker daemon failed to start. Please start Docker Desktop manually.",
            }

    # Step 5: Verify GPU passthrough
    status("[SETUP] Verifying GPU access in Docker containers...")
    gpu_result = resolve_gpu_verification()

    clear_setup_state()
    return {
        "ready": True,
        "needs_reboot": False,
        "gpu_verified": gpu_result["gpu_verified"],
        "gpu_docker_name": gpu_result["gpu_docker_name"],
        "message": "Docker is ready.",
    }


if __name__ == "__main__":
    result = full_bootstrap()
    print()
    print(f"Result: {result}")
