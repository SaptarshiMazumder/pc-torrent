"""
VastConfig — single source of truth for all Vast.ai configuration.

Reads environment variables and config.json once at import time.
Frozen dataclass: immutable, self-documenting, injectable.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config.json",
)


@dataclass(frozen=True)
class VastEndpoint:
    gpu_name: str
    label: str
    vram_gb: float
    render_speed: float = 1.0


@dataclass(frozen=True)
class VastConfig:
    api_key: str
    docker_image: str
    disk_gb: float
    max_price_per_gpu: float
    cpu_cores: int
    ram_gb: float
    poll_interval_sec: float
    startup_timeout_sec: float
    in_progress_stale_sec: float
    public_backend_url: str
    workers_per_endpoint: int
    heartbeat_interval_sec: int
    endpoints: tuple[VastEndpoint, ...] = field(default_factory=tuple)

    api_base: str = "https://console.vast.ai/api/v0"

    @classmethod
    def from_env(cls) -> VastConfig:
        endpoints = _parse_endpoints()
        return cls(
            api_key=os.getenv("VAST_API_KEY", ""),
            docker_image=(
                os.getenv("VAST_DOCKER_IMAGE")
                or os.getenv("MODAL_WORKER_IMAGE", "")
            ),
            disk_gb=_env_float("VAST_DISK_GB", 20.0),
            max_price_per_gpu=_env_float("VAST_MAX_PRICE_PER_GPU", 1.00),
            cpu_cores=_env_int("VAST_CPU_CORES", 8),
            ram_gb=_env_float("VAST_RAM_GB", 32.0),
            poll_interval_sec=_env_float("VAST_POLL_INTERVAL_SEC", 15.0),
            startup_timeout_sec=_env_float("VAST_STARTUP_TIMEOUT_SEC", 300.0),
            in_progress_stale_sec=_env_float("IN_PROGRESS_STALE_SEC", 90 * 60),
            public_backend_url=os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            workers_per_endpoint=_env_int("VAST_WORKERS_PER_ENDPOINT", 2),
            heartbeat_interval_sec=10,
            endpoints=tuple(endpoints),
        )

    def is_enabled(self) -> bool:
        return bool(self.api_key and self.docker_image and self.endpoints)


def _parse_endpoints() -> list[VastEndpoint]:
    try:
        with open(_CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning(f"Could not load {_CONFIG_PATH}: {exc}")
        return []

    results: list[VastEndpoint] = []
    for entry in cfg.get("vast_instances", []):
        gpu_name = entry.get("gpu_name", "").strip()
        if not gpu_name:
            continue
        results.append(VastEndpoint(
            gpu_name=gpu_name,
            label=entry.get("label", "").strip() or f"Vast {gpu_name}",
            vram_gb=float(entry.get("vram_gb", 24)),
            render_speed=float(entry.get("render_speed", 1.0)),
        ))
    return results


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default
