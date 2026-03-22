"""
Build the PC Rent Agent sidecar executable using PyInstaller.
Output is placed in desktop/src-tauri/binaries/ with Tauri's naming convention.
"""

import os
import shutil
import subprocess
import sys

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(AGENT_DIR)
BINARIES_DIR = os.path.join(PROJECT_ROOT, "desktop", "src-tauri")
SIDECAR_NAME = "pcrent-agent"
TARGET_TRIPLE = "x86_64-pc-windows-msvc"


def build():
    print("Building PC Rent Agent sidecar...")

    # Run PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", SIDECAR_NAME,
        "--distpath", os.path.join(AGENT_DIR, "dist"),
        "--workpath", os.path.join(AGENT_DIR, "build"),
        "--specpath", AGENT_DIR,
        "--clean",
        os.path.join(AGENT_DIR, "sidecar_main.py"),
    ]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=AGENT_DIR)
    if result.returncode != 0:
        print("PyInstaller build failed!")
        sys.exit(1)

    # Copy to Tauri binaries directory
    src = os.path.join(AGENT_DIR, "dist", f"{SIDECAR_NAME}.exe")
    if not os.path.exists(src):
        print(f"Build output not found: {src}")
        sys.exit(1)

    os.makedirs(BINARIES_DIR, exist_ok=True)
    dest = os.path.join(BINARIES_DIR, f"{SIDECAR_NAME}-{TARGET_TRIPLE}.exe")
    shutil.copy2(src, dest)

    size_mb = os.path.getsize(dest) / (1024 * 1024)
    print(f"Sidecar built: {dest} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    build()
