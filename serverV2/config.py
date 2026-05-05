"""Centralized configuration — composed from env vars. Zero legacy imports."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from serverV2.fleets.fleet_exception import FleetException

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)

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
    # Vast pricing is per-offer (marketplace) -- no longer in config.
    # FleetCapability.price_per_hour comes from the live offer's
    # ``dph_total`` at availability-build time.  This field stays as
    # an optional zero default for back-compat with anything that
    # still reads it.
    price_per_hour: float = 0.0

@dataclass(frozen=True)
class VastConfig:
    api_key: str
    docker_image: str
    provisioning_enabled: bool
    disk_gb: float
    secure_cloud_only: bool
    poll_interval_sec: float
    startup_timeout_sec: float
    in_progress_stale_sec: float
    public_backend_url: str
    heartbeat_interval_sec: int
    heartbeat_timeout_sec: float
    heartbeat_grace_sec: float
    max_parallel: int = 0
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
        # Tunables come from config.json's ``vast`` block; secrets / image
        # tags / per-environment toggles stay in env vars.
        block = _require_block("vast", config_json_path)
        return cls(
            api_key=_env_str("VAST_API_KEY"),
            docker_image=_env_str("VAST_DOCKER_IMAGE"),
            docker_image_eevee=_env_str("VAST_DOCKER_IMAGE_EEVEE") or None,
            provisioning_enabled=_require_field_bool(block, "vast", "provisioning_enabled"),
            disk_gb=_require_field_float(block, "vast", "disk_gb"),
            secure_cloud_only=_require_field_bool(block, "vast", "secure_cloud_only"),
            poll_interval_sec=_require_field_float(block, "vast", "poll_interval_sec"),
            startup_timeout_sec=_require_field_float(block, "vast", "startup_timeout_sec"),
            in_progress_stale_sec=_load_monitor_in_progress_stale_sec(config_json_path),
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            heartbeat_interval_sec=10,
            heartbeat_timeout_sec=_require_field_float(block, "vast", "heartbeat_timeout_sec"),
            heartbeat_grace_sec=_require_field_float(block, "vast", "heartbeat_grace_sec"),
            max_parallel=_require_fleet_int("vast", "max_parallel", config_json_path),
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


def _require_fleet_int(
    fleet_key: str, field_key: str, config_json_path: str | None = None,
) -> int:
    """Read a required int field from a fleet's config.json block.  Raises
    ``FleetException`` if the fleet block is missing, the field is missing,
    or the value isn't a valid positive int.  No defaults -- callers
    that need a knob set it explicitly in config.json.
    """
    cfg = _load_config_json(config_json_path)
    block = cfg.get(fleet_key)
    if not isinstance(block, dict):
        raise FleetException(
            f"config.json missing required block: {fleet_key!r}"
        )
    if field_key not in block:
        raise FleetException(
            f"config.json missing required key: {fleet_key}.{field_key}"
        )
    try:
        value = int(block[field_key])
    except (TypeError, ValueError) as exc:
        raise FleetException(
            f"config.json {fleet_key}.{field_key} is not a valid int: "
            f"{block[field_key]!r}"
        ) from exc
    if value < 1:
        raise FleetException(
            f"config.json {fleet_key}.{field_key} must be >= 1, got {value}"
        )
    return value


def _require_block(
    block_name: str, config_json_path: str | None = None,
) -> dict:
    """Load a top-level block from config.json.  Fails loud if missing —
    every caller of the typed-field helpers below expects to read REQUIRED
    fields, which is meaningless without the parent block.
    """
    cfg = _load_config_json(config_json_path)
    block = cfg.get(block_name)
    if not isinstance(block, dict):
        raise FleetException(f"config.json missing required block: {block_name!r}")
    return block


def _require_field_float(block: dict, block_name: str, key: str) -> float:
    if key not in block:
        raise FleetException(
            f"config.json missing required key: {block_name}.{key}"
        )
    try:
        return float(block[key])
    except (TypeError, ValueError) as exc:
        raise FleetException(
            f"config.json {block_name}.{key} is not a valid number: "
            f"{block[key]!r}"
        ) from exc


def _require_field_int(block: dict, block_name: str, key: str) -> int:
    if key not in block:
        raise FleetException(
            f"config.json missing required key: {block_name}.{key}"
        )
    try:
        return int(block[key])
    except (TypeError, ValueError) as exc:
        raise FleetException(
            f"config.json {block_name}.{key} is not a valid int: "
            f"{block[key]!r}"
        ) from exc


def _require_field_bool(block: dict, block_name: str, key: str) -> bool:
    if key not in block:
        raise FleetException(
            f"config.json missing required key: {block_name}.{key}"
        )
    value = block[key]
    if isinstance(value, bool):
        return value
    raise FleetException(
        f"config.json {block_name}.{key} is not a valid bool: {value!r}"
    )


def _optional_field_str(block: dict, key: str, default: str = "") -> str:
    """Read an OPTIONAL string field.  Used for values where 'absent' has
    a defined fallback semantic (e.g. ``modal.endpoint_url_prefix`` empty
    means 'derive from workspace + app_name').
    """
    value = block.get(key, default)
    if isinstance(value, str):
        return value.strip()
    return default


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


def _load_monitor_in_progress_stale_sec(
    config_json_path: str | None = None,
) -> float:
    """Read ``monitor.in_progress_stale_sec`` from config.json.

    Both Vast and Modal monitors use this — frame-progress hasn't
    advanced for this many seconds → kill the job.  Single value
    shared across fleets; per-fleet override would be a future
    nice-to-have but isn't needed today.

    Required field — fails loud if missing.  Tunables that shape
    job-failure semantics shouldn't have hidden defaults.
    """
    cfg = _load_config_json(config_json_path)
    block = cfg.get("monitor")
    if not isinstance(block, dict):
        raise FleetException("config.json missing required block: 'monitor'")
    if "in_progress_stale_sec" not in block:
        raise FleetException(
            "config.json missing required key: monitor.in_progress_stale_sec"
        )
    try:
        value = float(block["in_progress_stale_sec"])
    except (TypeError, ValueError) as exc:
        raise FleetException(
            f"config.json monitor.in_progress_stale_sec is not a valid number: "
            f"{block['in_progress_stale_sec']!r}"
        ) from exc
    if value <= 0:
        raise FleetException(
            f"config.json monitor.in_progress_stale_sec must be > 0, got {value}"
        )
    return value


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
        # ``price_per_hour`` is no longer required in config -- Vast
        # pricing is per-offer (marketplace).  Tolerate it being absent;
        # if present (legacy entries) it's loaded for back-compat.
        for required in ("vram_gb", "cpu_cores", "ram_gb", "render_speed"):
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
            price_per_hour=float(entry.get("price_per_hour") or 0.0),
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
    max_parallel: int = 0
    per_gpu_max_parallel: int = 0
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
        # Tunables come from config.json's ``modal`` block; secrets /
        # workspace / per-environment toggles stay in env vars.
        block = _require_block("modal")
        raw_timeout = _require_field_float(block, "modal", "dispatch_timeout_sec")
        return cls(
            token_id=_env_str("MODAL_TOKEN_ID"),
            token_secret=_env_str("MODAL_TOKEN_SECRET"),
            app_name=_env_str("MODAL_APP_NAME", "pcrent-render"),
            provisioning_enabled=_require_field_bool(block, "modal", "provisioning_enabled"),
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            heartbeat_interval_sec=10,
            monitor_interval_sec=30,
            dispatch_timeout_sec=None if raw_timeout <= 0 else raw_timeout,
            in_queue_timeout_sec=_require_field_float(block, "modal", "in_queue_timeout_sec"),
            in_progress_stale_sec=_load_monitor_in_progress_stale_sec(),
            endpoint_url_prefix=_optional_field_str(block, "endpoint_url_prefix").rstrip("/"),
            workspace=_env_str("MODAL_WORKSPACE").strip(),
            max_parallel=_require_fleet_int("modal", "max_parallel"),
            per_gpu_max_parallel=_require_fleet_int("modal", "per_gpu_max_parallel"),
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
    # Loading-phase watchdog: post-download / pre-first-frame budget.
    # ``allowed = clamp(estimated_startup_sec * multiplier, min, max)``.
    loading_multiplier: float = 3.0
    loading_phase_min_sec: float = 60.0
    loading_phase_max_sec: float = 1800.0
    hard_max_chunk_sec: float = 4 * 60 * 60.0

    @classmethod
    def from_env(cls) -> StallDetectionConfig:
        # Pre-render stall thresholds — all from config.json's ``stall``
        # block.  No env-var overrides; tunables live in one place.
        block = _require_block("stall")
        return cls(
            cpu_stall_threshold_pct=_require_field_float(block, "stall", "cpu_threshold_pct"),
            cpu_stall_window_sec=_require_field_float(block, "stall", "cpu_window_sec"),
            rss_noise_bytes=_require_field_int(block, "stall", "rss_noise_bytes"),
            download_bytes_stall_sec=_require_field_float(block, "stall", "download_bytes_stall_sec"),
            download_secs_per_gb=_require_field_float(block, "stall", "download_secs_per_gb"),
            download_phase_min_sec=_require_field_float(block, "stall", "download_phase_min_sec"),
            download_phase_max_sec=_require_field_float(block, "stall", "download_phase_max_sec"),
            loading_multiplier=_require_field_float(block, "stall", "loading_multiplier"),
            loading_phase_min_sec=_require_field_float(block, "stall", "loading_phase_min_sec"),
            loading_phase_max_sec=_require_field_float(block, "stall", "loading_phase_max_sec"),
            hard_max_chunk_sec=_require_field_float(block, "stall", "hard_max_chunk_sec"),
        )


@dataclass(frozen=True)
class StartupBufferConfig:
    """Per-fleet startup-time additive in seconds.  Modelled in the
    allocation time analyzer so short chunks don't make Vast look
    artificially fast (Vast pays 1-3 min of provisioning latency on top
    of the heaviness-based estimate).  Modal cold starts are similar but
    shorter.  Community machines are already running, so the buffer is
    zero by default.
    """
    vast: float = 180.0
    modal: float = 120.0
    community: float = 0.0

    def for_fleet(self, fleet: str | None) -> float:
        if not fleet:
            return 0.0
        if fleet == "vast_serverless":
            return self.vast
        if fleet == "modal_serverless":
            return self.modal
        if fleet == "community":
            return self.community
        return 0.0

    @classmethod
    def from_env(cls) -> StartupBufferConfig:
        block = _require_block("startup_buffer_sec")
        return cls(
            vast=_require_field_float(block, "startup_buffer_sec", "vast"),
            modal=_require_field_float(block, "startup_buffer_sec", "modal"),
            community=_require_field_float(block, "startup_buffer_sec", "community"),
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
    # CommunityMonitor's "agent never claimed the pending dispatch" detector.
    # Mirrors Modal's ``in_queue_timeout_sec`` and Vast's ``startup_timeout_sec``:
    # if a community ``jobs`` row sits in status='pending' for longer than this
    # without the agent claiming it, fail the chunk so the retry pipeline can
    # take over.  Threshold is 4x the agent's max polling interval.
    community_dispatch_claim_timeout_sec: int = 120
    # Per-hour cost stamped on every CommunityMachine.  Read by cost-aware
    # allocators (Phase 5+).  Loaded from config.json's ``community.price_per_hour``.
    community_price_per_hour: float = 1.0
    # Shared secret expected by ``POST /internal/orphan/{job_id}``.  The
    # backup_monitor service sends this header to identify itself.  Empty
    # string disables the endpoint (returns 503 to all callers).
    orphan_secret: str = ""
    stall: StallDetectionConfig = field(default_factory=StallDetectionConfig)
    startup_buffer: StartupBufferConfig = field(default_factory=StartupBufferConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        vast = VastConfig.from_env()
        modal = ModalConfig.from_env()
        community_block = _require_block("community")
        return cls(
            vast=vast,
            modal=modal,
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            community_price_per_hour=_load_community_price_per_hour(),
            community_dispatch_claim_timeout_sec=_require_field_int(
                community_block, "community", "dispatch_claim_timeout_sec",
            ),
            orphan_secret=_env_str("ORPHAN_SECRET", ""),
            stall=StallDetectionConfig.from_env(),
            startup_buffer=StartupBufferConfig.from_env(),
        )
