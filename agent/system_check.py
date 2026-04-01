"""
PC Rent Agent - System Requirements Checker
Validates Windows version, NVIDIA GPU presence, and driver version.
"""

import platform
import subprocess
import sys


# Minimum NVIDIA driver for WSL2 GPU passthrough
MIN_DRIVER_WIN10 = 510   # Windows 10
MIN_DRIVER_WIN11 = 470   # Windows 11 (WSL2 GPU support added in 470.76)
# Windows 10 21H2 build number
MIN_BUILD_WIN10 = 19044


def get_windows_version():
    """
    Returns (major, build, display) e.g. (10, 19044, "Windows 10 21H2")
    or (11, 22621, "Windows 11 23H2").
    """
    ver = platform.version()  # e.g. "10.0.22621"
    parts = ver.split(".")
    build = int(parts[2]) if len(parts) >= 3 else 0

    # Windows 11 starts at build 22000
    if build >= 22000:
        major = 11
    else:
        major = 10

    # Get display name from systeminfo or registry
    display = f"Windows {major} (build {build})"
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            product_name, _ = winreg.QueryValueEx(key, "ProductName")
            display_ver, _ = winreg.QueryValueEx(key, "DisplayVersion")
            display = f"{product_name} {display_ver}"
    except Exception:
        pass

    return major, build, display


def check_nvidia_gpu():
    """
    Check if an NVIDIA GPU is present and return info.
    Returns (present: bool, gpu_name: str, vram_gb: float, driver_version: str).
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            name = parts[0] if len(parts) > 0 else "Unknown"
            vram_mb = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
            driver = parts[2] if len(parts) > 2 else "Unknown"
            return True, name, round(vram_mb / 1024, 1), driver
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return False, "", 0.0, ""


def get_driver_major(driver_version):
    """Extract major version number from driver string like '560.94' → 560."""
    try:
        return int(driver_version.split(".")[0])
    except (ValueError, IndexError):
        return 0


def check_requirements():
    """
    Run all system requirement checks.
    Returns a dict with:
        ready: bool - True if all requirements met
        os_version: str - Display string for OS
        gpu_name: str - GPU model name
        gpu_vram_gb: float - GPU VRAM in GB
        nvidia_driver: str - Driver version string
        issues: list[str] - Human-readable issues (empty if ready)
    """
    issues = []

    # 1. Check Windows version
    win_major, win_build, os_display = get_windows_version()

    if win_major < 10:
        issues.append(f"Windows 10 or later required. Detected: {os_display}")
    elif win_major == 10 and win_build < MIN_BUILD_WIN10:
        issues.append(
            f"Windows 10 version 21H2 (build {MIN_BUILD_WIN10}+) required.\n"
            f"  Current: {os_display} (build {win_build}).\n"
            f"  Please run Windows Update."
        )

    # 2. Check NVIDIA GPU
    gpu_present, gpu_name, gpu_vram, driver_version = check_nvidia_gpu()

    if not gpu_present:
        issues.append(
            "NVIDIA GPU required for GPU rendering.\n"
            "  No NVIDIA GPU detected (nvidia-smi not found).\n"
            "  AMD GPU support is not yet available."
        )
    else:
        # 3. Check driver version (required for WSL2 GPU passthrough)
        driver_major = get_driver_major(driver_version)
        if win_major == 10 and driver_major < MIN_DRIVER_WIN10:
            issues.append(
                f"NVIDIA driver {MIN_DRIVER_WIN10}+ required for GPU in Docker on Windows 10.\n"
                f"  Current driver: {driver_version} (major: {driver_major}).\n"
                f"  Update at: https://www.nvidia.com/drivers"
            )
        elif win_major >= 11 and driver_major < MIN_DRIVER_WIN11:
            issues.append(
                f"NVIDIA driver {MIN_DRIVER_WIN11}+ required for GPU in Docker on Windows 11.\n"
                f"  Current driver: {driver_version} (major: {driver_major}).\n"
                f"  Update at: https://www.nvidia.com/drivers"
            )

    return {
        "ready": len(issues) == 0,
        "os_version": os_display,
        "gpu_name": gpu_name,
        "gpu_vram_gb": gpu_vram,
        "nvidia_driver": driver_version,
        "issues": issues,
    }


def print_check_results():
    """Print system check results to console."""
    result = check_requirements()

    print("=== System Requirements Check ===")
    print(f"  OS:     {result['os_version']}")
    if result["gpu_name"]:
        print(f"  GPU:    {result['gpu_name']} ({result['gpu_vram_gb']} GB VRAM)")
        print(f"  Driver: {result['nvidia_driver']}")
    else:
        print("  GPU:    Not detected")
    print()

    if result["ready"]:
        print("[OK] All system requirements met.")
    else:
        print("[FAIL] System requirements not met:")
        for issue in result["issues"]:
            for line in issue.split("\n"):
                print(f"  {line}")
        print()

    return result


if __name__ == "__main__":
    result = print_check_results()
    sys.exit(0 if result["ready"] else 1)
