"""
PC Rent Agent - Configuration Management
Persists agent state in %APPDATA%/PCRent/.
"""

import json
import os
from pathlib import Path


def _get_config_dir():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "PCRent"
    return Path.home() / ".pcrent"


CONFIG_DIR = _get_config_dir()


def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def get_config_path(filename):
    return CONFIG_DIR / filename


# -----------------------------------------------
# Machine ID (persists across restarts)
# -----------------------------------------------
def load_machine_id():
    path = get_config_path("machine_id")
    if path.exists():
        return path.read_text().strip()
    return None


def save_machine_id(machine_id):
    ensure_config_dir()
    get_config_path("machine_id").write_text(machine_id)


# -----------------------------------------------
# Docker image hash (tracks cached image version)
# -----------------------------------------------
def load_image_sha():
    path = get_config_path("image.sha256")
    if path.exists():
        return path.read_text().strip()
    return None


def save_image_sha(sha):
    ensure_config_dir()
    get_config_path("image.sha256").write_text(sha)


def clear_image_sha():
    path = get_config_path("image.sha256")
    if path.exists():
        path.unlink()


# -----------------------------------------------
# GPU-in-Docker verification cache
# -----------------------------------------------
def load_gpu_check_cache():
    path = get_config_path("gpu_check.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_gpu_check_cache(cache):
    ensure_config_dir()
    get_config_path("gpu_check.json").write_text(json.dumps(cache, indent=2))


def clear_gpu_check_cache():
    path = get_config_path("gpu_check.json")
    if path.exists():
        path.unlink()


# -----------------------------------------------
# Setup state (tracks first-time setup progress for reboot resume)
# -----------------------------------------------
def load_setup_state():
    path = get_config_path("setup_state.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_setup_state(state):
    ensure_config_dir()
    get_config_path("setup_state.json").write_text(json.dumps(state, indent=2))


def clear_setup_state():
    path = get_config_path("setup_state.json")
    if path.exists():
        path.unlink()


# -----------------------------------------------
# General config (backend URL, etc.)
# -----------------------------------------------
def load_config():
    path = get_config_path("config.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(config):
    ensure_config_dir()
    get_config_path("config.json").write_text(json.dumps(config, indent=2))
