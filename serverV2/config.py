"""Centralized configuration — composed from env vars. Zero legacy imports."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s='%s', using default %s", name, raw, default)
        return default

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid %s='%s', using default %s", name, raw, default)
        return default

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default

def _env_csv_set(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


# ---------------------------------------------------------------------------
# Vast
# ---------------------------------------------------------------------------

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
    provisioning_enabled: bool
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
    heartbeat_timeout_sec: float
    heartbeat_grace_sec: float
    endpoints: tuple[VastEndpoint, ...] = field(default_factory=tuple)
    api_base: str = "https://console.vast.ai/api/v0"

    def is_enabled(self) -> bool:
        return bool(self.provisioning_enabled and self.api_key and self.docker_image and self.endpoints)

    @classmethod
    def from_env(cls, config_json_path: str | None = None) -> VastConfig:
        return cls(
            api_key=_env_str("VAST_API_KEY"),
            docker_image=_env_str("VAST_DOCKER_IMAGE") or _env_str("MODAL_WORKER_IMAGE"),
            provisioning_enabled=_env_bool("VAST_PROVISIONING_ENABLED", True),
            disk_gb=_env_float("VAST_DISK_GB", 20.0),
            max_price_per_gpu=_env_float("VAST_MAX_PRICE_PER_GPU", 1.00),
            cpu_cores=_env_int("VAST_CPU_CORES", 8),
            ram_gb=_env_float("VAST_RAM_GB", 32.0),
            poll_interval_sec=_env_float("VAST_POLL_INTERVAL_SEC", 15.0),
            startup_timeout_sec=_env_float("VAST_STARTUP_TIMEOUT_SEC", 300.0),
            in_progress_stale_sec=_env_float("IN_PROGRESS_STALE_SEC", 10 * 60),
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            workers_per_endpoint=_env_int("VAST_WORKERS_PER_ENDPOINT", 2),
            heartbeat_interval_sec=10,
            heartbeat_timeout_sec=_env_float("VAST_HEARTBEAT_TIMEOUT_SEC", 45.0),
            heartbeat_grace_sec=_env_float("VAST_HEARTBEAT_GRACE_SEC", 90.0),
            endpoints=tuple(_parse_vast_endpoints(config_json_path)),
        )


def _parse_vast_endpoints(config_json_path: str | None = None) -> list[VastEndpoint]:
    path = config_json_path or os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("Could not load %s: %s", path, exc)
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


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModalEndpoint:
    gpu_type: str
    label: str

@dataclass(frozen=True)
class ModalConfig:
    token_id: str
    token_secret: str
    app_name: str
    provisioning_enabled: bool
    disabled_gpu_types: frozenset[str]
    gpu_vram_gb: float
    cpu_cores: int
    ram_gb: float
    public_backend_url: str
    workers_per_endpoint: int
    heartbeat_interval_sec: int
    monitor_interval_sec: int
    dispatch_timeout_sec: float | None
    in_queue_timeout_sec: float
    in_progress_stale_sec: float
    endpoint_url_prefix: str
    workspace: str
    endpoints: tuple[ModalEndpoint, ...] = field(default_factory=tuple)

    def is_enabled(self) -> bool:
        return bool(self.provisioning_enabled and self.token_id and self.token_secret and self.endpoints)

    def endpoint_url(self, gpu_type: str) -> str:
        prefix = self.endpoint_url_prefix
        if prefix:
            if "{gpu_type}" in prefix:
                return prefix.format(gpu_type=gpu_type)
            if f"-render-{gpu_type}.modal.run" in prefix:
                return prefix
            if prefix.endswith(".modal.run"):
                return prefix.replace(".modal.run", f"-render-{gpu_type}.modal.run")
            if "--" in prefix:
                return f"{prefix}-render-{gpu_type}.modal.run"
            return f"{prefix}/render-{gpu_type}"
        if self.workspace:
            return f"https://{self.workspace}--{self.app_name}-render-{gpu_type}.modal.run"
        raise ValueError("Cannot construct Modal endpoint URL: set MODAL_ENDPOINT_URL_PREFIX or MODAL_WORKSPACE")

    @classmethod
    def from_env(cls) -> ModalConfig:
        disabled = _env_csv_set("MODAL_DISABLED_GPU_TYPES")
        raw_timeout = _env_float("MODAL_DISPATCH_TIMEOUT_SEC", 6 * 60 * 60)
        return cls(
            token_id=_env_str("MODAL_TOKEN_ID"),
            token_secret=_env_str("MODAL_TOKEN_SECRET"),
            app_name=_env_str("MODAL_APP_NAME", "pcrent-render"),
            provisioning_enabled=_env_bool("MODAL_PROVISIONING_ENABLED", True),
            disabled_gpu_types=frozenset(disabled),
            gpu_vram_gb=_env_float("MODAL_GPU_VRAM_GB", 24.0),
            cpu_cores=_env_int("MODAL_CPU_CORES", 16),
            ram_gb=_env_float("MODAL_RAM_GB", 64.0),
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            workers_per_endpoint=_env_int("MODAL_WORKERS_PER_ENDPOINT", 3),
            heartbeat_interval_sec=10,
            monitor_interval_sec=30,
            dispatch_timeout_sec=None if raw_timeout <= 0 else raw_timeout,
            in_queue_timeout_sec=_env_float("IN_QUEUE_TIMEOUT_SEC", 120),
            in_progress_stale_sec=_env_float("IN_PROGRESS_STALE_SEC", 10 * 60),
            endpoint_url_prefix=_env_str("MODAL_ENDPOINT_URL_PREFIX").strip().rstrip("/"),
            workspace=_env_str("MODAL_WORKSPACE").strip(),
            endpoints=tuple(_parse_modal_endpoints(disabled)),
        )

def _normalize_gpu_type(gpu_type: str) -> str | None:
    v = gpu_type.strip().lower()
    return "a10g" if v in {"a10", "a10g"} else None

def _parse_modal_endpoints(disabled: set[str]) -> list[ModalEndpoint]:
    raw = _env_str("MODAL_ENDPOINTS").strip()
    if not raw:
        return []
    results: list[ModalEndpoint] = []
    seen: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            raw_type, label = part.split(":", 1)
            gpu_type = _normalize_gpu_type(raw_type.strip())
        else:
            gpu_type = _normalize_gpu_type(part)
            label = f"Modal {part.strip().upper()}" if gpu_type else ""
        if not gpu_type or gpu_type in disabled or gpu_type in seen:
            continue
        results.append(ModalEndpoint(gpu_type=gpu_type, label=label.strip()))
        seen.add(gpu_type)
    return results


# ---------------------------------------------------------------------------
# App-level config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    vast: VastConfig
    modal: ModalConfig
    public_backend_url: str
    min_frames_per_worker: int = 2
    machine_stale_seconds: int = 15
    failover_stale_seconds: int = 30

    @classmethod
    def from_env(cls) -> AppConfig:
        vast = VastConfig.from_env()
        modal = ModalConfig.from_env()
        return cls(
            vast=vast,
            modal=modal,
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
        )
