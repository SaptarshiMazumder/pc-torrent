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

# ---------------------------------------------------------------------------
# Vast
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VastEndpoint:
    gpu_name: str
    label: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    price_per_hour: float

@dataclass(frozen=True)
class VastConfig:
    api_key: str
    docker_image: str
    provisioning_enabled: bool
    disk_gb: float
    max_price_per_gpu: float
    secure_cloud_only: bool
    poll_interval_sec: float
    startup_timeout_sec: float
    in_progress_stale_sec: float
    public_backend_url: str
    heartbeat_interval_sec: int
    heartbeat_timeout_sec: float
    heartbeat_grace_sec: float
    max_parallel: int = 15
    endpoints: tuple[VastEndpoint, ...] = field(default_factory=tuple)
    api_base: str = "https://console.vast.ai/api/v0"
    # Separate image for EEVEE renders (NVIDIA EGL ICD + GLVND).  When unset
    # we fall back to ``docker_image`` and EEVEE chunks land on the cycles
    # image, which works only on hosts that already have a usable EGL stack.
    docker_image_eevee: str | None = None

    def is_enabled(self) -> bool:
        return bool(self.provisioning_enabled and self.api_key and self.docker_image and self.endpoints)

    def image_for_engine(self, engine: str | None) -> str:
        """Return the docker image to rent based on the render engine.
        EEVEE prefers ``docker_image_eevee`` when set; everything else (and
        the EEVEE fallback when no eevee image is configured) uses
        ``docker_image``.
        """
        if engine and engine.upper() in {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}:
            return self.docker_image_eevee or self.docker_image
        return self.docker_image

    @classmethod
    def from_env(cls, config_json_path: str | None = None) -> VastConfig:
        return cls(
            api_key=_env_str("VAST_API_KEY"),
            docker_image=_env_str("VAST_DOCKER_IMAGE"),
            docker_image_eevee=_env_str("VAST_DOCKER_IMAGE_EEVEE") or None,
            provisioning_enabled=_env_bool("VAST_PROVISIONING_ENABLED", True),
            disk_gb=_env_float("VAST_DISK_GB", 20.0),
            max_price_per_gpu=_env_float("VAST_MAX_PRICE_PER_GPU", 1.00),
            secure_cloud_only=_env_bool("VAST_SECURE_CLOUD_ONLY", True),
            poll_interval_sec=_env_float("VAST_POLL_INTERVAL_SEC", 15.0),
            startup_timeout_sec=_env_float("VAST_STARTUP_TIMEOUT_SEC", 300.0),
            in_progress_stale_sec=_env_float("IN_PROGRESS_STALE_SEC", 30 * 60),
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            heartbeat_interval_sec=10,
            heartbeat_timeout_sec=_env_float("VAST_HEARTBEAT_TIMEOUT_SEC", 45.0),
            heartbeat_grace_sec=_env_float("VAST_HEARTBEAT_GRACE_SEC", 90.0),
            max_parallel=_load_fleet_max_parallel("vast", config_json_path),
            endpoints=tuple(_parse_vast_endpoints(config_json_path)),
        )


def _load_config_json(config_json_path: str | None = None) -> dict:
    path = config_json_path or os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("Could not load %s: %s", path, exc)
        return {}


def _load_fleet_max_parallel(
    fleet_key: str, config_json_path: str | None = None,
) -> int:
    """Read ``<fleet>.max_parallel`` from config.json.  Default 15 if absent."""
    cfg = _load_config_json(config_json_path)
    block = cfg.get(fleet_key) or {}
    try:
        value = int(block.get("max_parallel", 15))
    except (TypeError, ValueError):
        value = 15
    return max(1, value)


def _load_community_price_per_hour(config_json_path: str | None = None) -> float:
    """Read ``community.price_per_hour`` from config.json.  Default 1.00 if absent.
    Used by the bootstrap wiring so every CommunityMachine carries a price the
    cost-aware allocators (Phase 5+) can read.
    """
    cfg = _load_config_json(config_json_path)
    block = cfg.get("community") or {}
    try:
        value = float(block.get("price_per_hour", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return max(0.0, value)


def _parse_vast_endpoints(config_json_path: str | None = None) -> list[VastEndpoint]:
    cfg = _load_config_json(config_json_path)
    if not cfg:
        return []
    results: list[VastEndpoint] = []
    for entry in cfg.get("vast_instances", []):
        gpu_name = str(entry.get("gpu_name", "")).strip()
        if not gpu_name:
            raise ValueError(
                f"vast_instances entry has missing gpu_name: {entry!r}"
            )
        for required in ("vram_gb", "cpu_cores", "ram_gb", "render_speed", "price_per_hour"):
            if required not in entry:
                raise ValueError(
                    f"vast_instances entry {gpu_name!r} is missing required field {required!r}"
                )
        results.append(VastEndpoint(
            gpu_name=gpu_name,
            label=str(entry.get("label", "")).strip() or f"Vast {gpu_name}",
            vram_gb=float(entry["vram_gb"]),
            cpu_cores=int(entry["cpu_cores"]),
            ram_gb=float(entry["ram_gb"]),
            render_speed=float(entry["render_speed"]),
            price_per_hour=float(entry["price_per_hour"]),
        ))
    return results


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModalEndpoint:
    gpu_type: str
    label: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    price_per_hour: float

@dataclass(frozen=True)
class ModalConfig:
    token_id: str
    token_secret: str
    app_name: str
    provisioning_enabled: bool
    public_backend_url: str
    heartbeat_interval_sec: int
    monitor_interval_sec: int
    dispatch_timeout_sec: float | None
    in_queue_timeout_sec: float
    in_progress_stale_sec: float
    endpoint_url_prefix: str
    workspace: str
    max_parallel: int = 15
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
        raw_timeout = _env_float("MODAL_DISPATCH_TIMEOUT_SEC", 6 * 60 * 60)
        return cls(
            token_id=_env_str("MODAL_TOKEN_ID"),
            token_secret=_env_str("MODAL_TOKEN_SECRET"),
            app_name=_env_str("MODAL_APP_NAME", "pcrent-render"),
            provisioning_enabled=_env_bool("MODAL_PROVISIONING_ENABLED", True),
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            heartbeat_interval_sec=10,
            monitor_interval_sec=30,
            dispatch_timeout_sec=None if raw_timeout <= 0 else raw_timeout,
            in_queue_timeout_sec=_env_float("IN_QUEUE_TIMEOUT_SEC", 120),
            in_progress_stale_sec=_env_float("IN_PROGRESS_STALE_SEC", 30 * 60),
            endpoint_url_prefix=_env_str("MODAL_ENDPOINT_URL_PREFIX").strip().rstrip("/"),
            workspace=_env_str("MODAL_WORKSPACE").strip(),
            max_parallel=_load_fleet_max_parallel("modal"),
            endpoints=tuple(_parse_modal_endpoints()),
        )

def _normalize_gpu_type(gpu_type: str) -> str | None:
    """Sanitize the gpu_type string from config.json into a Python-identifier
    form usable as a Modal function-name suffix (and therefore URL path).
    Returns None for empty/invalid input — the boot validator will catch
    any typo by HTTP-404'ing the resulting endpoint URL.
    """
    v = gpu_type.strip().lower().replace("-", "_")
    if not v or not all(c.isalnum() or c == "_" for c in v):
        return None
    return v


def _parse_modal_endpoints(
    config_json_path: str | None = None,
) -> list[ModalEndpoint]:
    """Read Modal endpoints from ``config.json``.  Single source of truth
    for the fleet — every required field (``gpu_type``, ``vram_gb``) MUST
    be present in the JSON entry; missing values raise at boot.
    """
    cfg = _load_config_json(config_json_path)
    if not cfg:
        return []
    results: list[ModalEndpoint] = []
    seen: set[str] = set()
    for entry in cfg.get("modal_instances", []):
        raw_type = str(entry.get("gpu_type", "")).strip()
        gpu_type = _normalize_gpu_type(raw_type)
        if not gpu_type:
            raise ValueError(
                f"modal_instances entry has invalid or missing gpu_type: {entry!r}"
            )
        if gpu_type in seen:
            raise ValueError(
                f"modal_instances entry duplicates gpu_type={gpu_type!r}"
            )
        for required in ("vram_gb", "cpu_cores", "ram_gb", "render_speed", "price_per_hour"):
            if required not in entry:
                raise ValueError(
                    f"modal_instances entry {gpu_type!r} is missing required field {required!r}"
                )
        label = (
            str(entry.get("label", "")).strip()
            or f"Modal {raw_type.upper()}"
        )
        results.append(ModalEndpoint(
            gpu_type=gpu_type,
            label=label,
            vram_gb=float(entry["vram_gb"]),
            cpu_cores=int(entry["cpu_cores"]),
            ram_gb=float(entry["ram_gb"]),
            render_speed=float(entry["render_speed"]),
            price_per_hour=float(entry["price_per_hour"]),
        ))
        seen.add(gpu_type)
    return results


# ---------------------------------------------------------------------------
# App-level config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StallDetectionConfig:
    """Thresholds for the pre-render stall detector.  Identical across
    fleets today; the per-fleet builder lets us split later if Vast and
    community need different timeouts."""
    cpu_stall_threshold_pct: float = 5.0
    cpu_stall_window_sec: float = 180.0
    rss_noise_bytes: int = 64 * 1024 * 1024
    download_bytes_stall_sec: float = 300.0
    download_secs_per_gb: float = 120.0
    download_phase_min_sec: float = 300.0
    download_phase_max_sec: float = 1800.0
    hard_max_chunk_sec: float = 4 * 60 * 60.0

    @classmethod
    def from_env(cls) -> StallDetectionConfig:
        return cls(
            cpu_stall_threshold_pct=_env_float("STALL_CPU_PCT", 5.0),
            cpu_stall_window_sec=_env_float("STALL_WINDOW_SEC", 180.0),
            rss_noise_bytes=_env_int("STALL_RSS_NOISE_BYTES", 64 * 1024 * 1024),
            download_bytes_stall_sec=_env_float("STALL_DOWNLOAD_BYTES_SEC", 300.0),
            download_secs_per_gb=_env_float("STALL_DOWNLOAD_SECS_PER_GB", 120.0),
            download_phase_min_sec=_env_float("STALL_DOWNLOAD_PHASE_MIN_SEC", 300.0),
            download_phase_max_sec=_env_float("STALL_DOWNLOAD_PHASE_MAX_SEC", 1800.0),
            hard_max_chunk_sec=_env_float("STALL_HARD_MAX_CHUNK_SEC", 4 * 60 * 60.0),
        )


@dataclass(frozen=True)
class AppConfig:
    vast: VastConfig
    modal: ModalConfig
    public_backend_url: str
    min_frames_per_worker: int = 2
    # Allocator threshold: how recently a community machine must have
    # heartbeated to be eligible for new dispatches.  Tight (15s) so we
    # don't dispatch to a dead PC.
    machine_stale_seconds: int = 15
    # CommunityMonitor's "machine went offline mid-render" detection.
    # Wider than allocator so a brief network blip mid-render doesn't
    # immediately fail the chunk.
    failover_stale_seconds: int = 30
    # CommunityMonitor's "demote ghost machines back to idle" threshold.
    # Wider still: must be >> heartbeat_interval (10s) + first-heartbeat
    # lag after /available transition (~10s) so a normally-connecting
    # agent can't be demoted in the race window between declaring
    # available and its first ZADD landing.  Demote only fires for
    # machines that have been silent long enough to be considered
    # genuinely crashed.
    community_machine_demote_seconds: int = 90
    # Per-hour cost stamped on every CommunityMachine.  Read by cost-aware
    # allocators (Phase 5+).  Loaded from config.json's ``community.price_per_hour``.
    community_price_per_hour: float = 1.0
    # Shared secret expected by ``POST /internal/orphan/{job_id}``.  The
    # backup_monitor service sends this header to identify itself.  Empty
    # string disables the endpoint (returns 503 to all callers).
    orphan_secret: str = ""
    stall: StallDetectionConfig = field(default_factory=StallDetectionConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        vast = VastConfig.from_env()
        modal = ModalConfig.from_env()
        return cls(
            vast=vast,
            modal=modal,
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            community_price_per_hour=_load_community_price_per_hour(),
            orphan_secret=_env_str("ORPHAN_SECRET", ""),
            stall=StallDetectionConfig.from_env(),
        )
