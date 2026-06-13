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
    # ``config.json`` lives at ``serverV2/config.json`` -- one directory
    # up from this module after the move into the ``serverV2/config/``
    # package.  Resolve relative to ``__file__`` so the lookup works
    # under any working directory (uvicorn / pytest / Cloud Run).
    path = config_json_path or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config.json",
    )
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


def _require_subblock(
    parent_name: str, sub_name: str, config_json_path: str | None = None,
) -> dict:
    """Load a nested block ``<parent>.<sub>`` from config.json.  Used by
    the frame-allocation loaders so the dotted path ``frame_allocation.
    weights`` is one helper call instead of two manual lookups.
    """
    parent = _require_block(parent_name, config_json_path)
    sub = parent.get(sub_name)
    if not isinstance(sub, dict):
        raise FleetException(
            f"config.json missing required block: {parent_name}.{sub_name}"
        )
    return sub


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
    # Hardcoded-but-Firestore-tunable availability window stamped onto
    # every Modal FleetCapability the planner sees.  Modal endpoints
    # don't expose a per-call lease horizon, so we treat the entire
    # fleet as "available for this many seconds" and let the planner's
    # time filter reject chunks whose render time exceeds the window.
    availability_sec: float = 14400.0
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
            availability_sec=_require_field_float(block, "modal", "availability_sec"),
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
class VramFleetBoostConfig:
    """Per-fleet multiplier on a target's effective VRAM during the
    eligibility check.  The planner's check is::

        target.vram_gb * fleet_boost >= required_vram * vram_safety_factor

    Vast / Modal default to 1.0 (treat the GPU's real VRAM at face value)
    because spinning up paid compute to hit an OOM costs real money.
    Community defaults > 1.0 (treat the user's own hardware as if it had
    more VRAM than the estimator's pessimistic floor) -- the trade-off
    is a rare OOM that the retry pipeline reassigns to a bigger card,
    in exchange for hitting the user's idle hardware much more often.
    """
    vast: float = 1.0
    modal: float = 1.0
    community: float = 1.0

    def for_fleet(self, fleet: str | None) -> float:
        if not fleet:
            return 1.0
        if fleet == "vast_serverless":
            return self.vast
        if fleet == "modal_serverless":
            return self.modal
        if fleet == "community":
            return self.community
        return 1.0

    @classmethod
    def from_env(cls) -> VramFleetBoostConfig:
        block = _require_subblock("frame_allocation", "vram_fleet_boost")
        ctx = "frame_allocation.vram_fleet_boost"
        return cls(
            vast=_require_field_float(block, ctx, "vast"),
            modal=_require_field_float(block, ctx, "modal"),
            community=_require_field_float(block, ctx, "community"),
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
        block = _require_subblock("frame_allocation", "startup_buffer_sec")
        ctx = "frame_allocation.startup_buffer_sec"
        return cls(
            vast=_require_field_float(block, ctx, "vast"),
            modal=_require_field_float(block, ctx, "modal"),
            community=_require_field_float(block, ctx, "community"),
        )


@dataclass(frozen=True)
class EngineFactors:
    """Per-engine multipliers for heavy-feature flags + adaptive
    sampling.  All express "render takes N times longer when this flag
    is on" relative to the same scene without it.  ``adaptive_sampling``
    is < 1.0 (it speeds renders up).
    """
    subdivision: float
    displacement: float
    particles: float
    subsurface: float
    volumetrics: float
    adaptive_sampling: float


@dataclass(frozen=True)
class RenderStartupSec:
    """Per-chunk startup additive components in seconds."""
    baseline: float
    download_per_gb: float
    bvh_per_million_verts: float
    texture_upload_per_gb: float
    shader_compile_base: float
    shader_compile_per_node: float
    max_total: float


@dataclass(frozen=True)
class SceneScalingConfig:
    """Per-engine pixel + sample scaling curves.

    Each axis follows ``factor = max(min, (value / baseline) ** exponent)``.
    ``exponent = 1.0`` is linear (every doubling adds 100% time);
    ``0.5`` is sqrt (every doubling adds ~41%);
    ``0.0`` is flat (no scaling).

    Cycles defaults to slight-sublinear (BVH amortizes, samples are
    path-traced and dominate).  EEVEE defaults to strongly-sublinear
    (TAA samples are nearly free; rasterization is mostly fragment-shader
    bound, scaling weakly with resolution).  Wrong defaults caused
    Eevee scenes to be estimated 10–50× over actual; the new defaults
    track real engine behavior.
    """
    baseline_pixels: float
    pixel_curve_exponent: float
    min_pixel_factor: float
    baseline_samples: float
    sample_curve_exponent: float
    min_sample_factor: float


@dataclass(frozen=True)
class RenderTimeConfig:
    """Calibration knobs for ``allocation_time_analyzer``.  Lifted from
    module constants so they can be tuned via config.json without code
    changes.  Telemetry-driven calibration (separate workstream) will
    eventually fit these from real render data; until then they're
    eyeballed defaults.
    """
    baseline_sec_cycles: float
    baseline_sec_eevee: float
    factors_cycles: EngineFactors
    factors_eevee: EngineFactors
    scene_scaling_cycles: SceneScalingConfig
    scene_scaling_eevee: SceneScalingConfig
    startup: RenderStartupSec

    @classmethod
    def from_env(cls) -> RenderTimeConfig:
        block = _require_subblock("frame_allocation", "render_time")
        ctx = "frame_allocation.render_time"

        def _factors(key: str) -> EngineFactors:
            sub = block.get(key)
            if not isinstance(sub, dict):
                raise FleetException(f"{ctx}.{key} missing or not an object")
            sub_ctx = f"{ctx}.{key}"
            return EngineFactors(
                subdivision=_require_field_float(sub, sub_ctx, "subdivision"),
                displacement=_require_field_float(sub, sub_ctx, "displacement"),
                particles=_require_field_float(sub, sub_ctx, "particles"),
                subsurface=_require_field_float(sub, sub_ctx, "subsurface"),
                volumetrics=_require_field_float(sub, sub_ctx, "volumetrics"),
                adaptive_sampling=_require_field_float(sub, sub_ctx, "adaptive_sampling"),
            )

        def _scene_scaling(key: str, defaults: SceneScalingConfig) -> SceneScalingConfig:
            # Backwards-compat: callers (Firestore docs especially) that
            # predate this section get the engine-appropriate default.
            sub = block.get(key)
            if not isinstance(sub, dict):
                return defaults
            sub_ctx = f"{ctx}.{key}"
            return SceneScalingConfig(
                baseline_pixels=_require_field_float(sub, sub_ctx, "baseline_pixels"),
                pixel_curve_exponent=_require_field_float(sub, sub_ctx, "pixel_curve_exponent"),
                min_pixel_factor=_require_field_float(sub, sub_ctx, "min_pixel_factor"),
                baseline_samples=_require_field_float(sub, sub_ctx, "baseline_samples"),
                sample_curve_exponent=_require_field_float(sub, sub_ctx, "sample_curve_exponent"),
                min_sample_factor=_require_field_float(sub, sub_ctx, "min_sample_factor"),
            )

        startup_block = block.get("startup_sec")
        if not isinstance(startup_block, dict):
            raise FleetException(f"{ctx}.startup_sec missing or not an object")
        startup_ctx = f"{ctx}.startup_sec"

        return cls(
            baseline_sec_cycles=_require_field_float(block, ctx, "baseline_sec_cycles"),
            baseline_sec_eevee=_require_field_float(block, ctx, "baseline_sec_eevee"),
            factors_cycles=_factors("factors_cycles"),
            factors_eevee=_factors("factors_eevee"),
            scene_scaling_cycles=_scene_scaling(
                "scene_scaling_cycles",
                SceneScalingConfig(
                    baseline_pixels=1920 * 1080,
                    pixel_curve_exponent=0.85,
                    min_pixel_factor=0.25,
                    baseline_samples=1024.0,
                    sample_curve_exponent=0.9,
                    min_sample_factor=0.0,
                ),
            ),
            scene_scaling_eevee=_scene_scaling(
                "scene_scaling_eevee",
                SceneScalingConfig(
                    baseline_pixels=1920 * 1080,
                    pixel_curve_exponent=0.5,
                    min_pixel_factor=0.25,
                    baseline_samples=64.0,
                    sample_curve_exponent=0.4,
                    min_sample_factor=0.5,
                ),
            ),
            startup=RenderStartupSec(
                baseline=_require_field_float(startup_block, startup_ctx, "baseline"),
                download_per_gb=_require_field_float(startup_block, startup_ctx, "download_per_gb"),
                bvh_per_million_verts=_require_field_float(startup_block, startup_ctx, "bvh_per_million_verts"),
                texture_upload_per_gb=_require_field_float(startup_block, startup_ctx, "texture_upload_per_gb"),
                shader_compile_base=_require_field_float(startup_block, startup_ctx, "shader_compile_base"),
                shader_compile_per_node=_require_field_float(startup_block, startup_ctx, "shader_compile_per_node"),
                max_total=_require_field_float(startup_block, startup_ctx, "max_total"),
            ),
        )


@dataclass(frozen=True)
class FailureRateConfig:
    """Per-fleet first-attempt failure probability (0..1).  Used to
    widen the dry-run cost and wall-time estimates so the UI doesn't
    advertise the happy-path-only number.  Not used in allocation
    scoring -- this is purely a display projection.

    Vast is the highest because the marketplace mixes hosts of
    varying driver / network quality.  Modal is lowest (managed,
    homogeneous).  Community sits in between (user PCs are stable
    but vary in network reliability).
    """
    vast: float = 0.20
    modal: float = 0.05
    community: float = 0.10

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
    def from_env(cls) -> FailureRateConfig:
        block = _require_subblock("frame_allocation", "failure_rate")
        ctx = "frame_allocation.failure_rate"
        return cls(
            vast=_require_field_float(block, ctx, "vast"),
            modal=_require_field_float(block, ctx, "modal"),
            community=_require_field_float(block, ctx, "community"),
        )


def _load_allocation_weights() -> "AllocationWeights":
    """Read the allocator's tunables from
    ``frame_allocation.weights`` in config.json and construct an
    :class:`AllocationWeights`.  Imported lazily to avoid a circular
    import (allocation_weights imports nothing from config; config
    imports the dataclass shape only).
    """
    from serverV2.allocation.allocation_strategies.allocation_weights import (
        AllocationWeights,
    )
    block = _require_subblock("frame_allocation", "weights")
    ctx = "frame_allocation.weights"
    return AllocationWeights(
        speed_weight=_require_field_float(block, ctx, "speed_weight"),
        cuda_weight=_require_field_float(block, ctx, "cuda_weight"),
        os_weight=_require_field_float(block, ctx, "os_weight"),
        max_targets=_require_field_int(block, ctx, "max_targets"),
        min_frames_per_chunk=_require_field_int(block, ctx, "min_frames_per_chunk"),
        fleet_diversification_cap=_require_field_float(block, ctx, "fleet_diversification_cap"),
        gpu_type_diversification_cap=_require_field_float(block, ctx, "gpu_type_diversification_cap"),
        vram_safety_factor=_require_field_float(block, ctx, "vram_safety_factor"),
        startup_amortization_ratio=_require_field_float(block, ctx, "startup_amortization_ratio"),
        time_safety_factor=_require_field_float(block, ctx, "time_safety_factor"),
        time_headroom_falloff=_require_field_float(block, ctx, "time_headroom_falloff"),
        secondary_feature_credit=_require_field_float(
            block, ctx, "secondary_feature_credit",
        ),
        heavy_multiplier_cap=_require_field_float(
            block, ctx, "heavy_multiplier_cap",
        ),
        priority_cost_multipliers=_load_priority_cost_multipliers(block, ctx),
    )


def _load_priority_cost_multipliers(block: dict, ctx: str) -> dict[str, float]:
    """Load the per-priority cost multipliers from a weights block.

    Defaults to 1.0 / 1.1 / 1.2 when the section is absent so existing
    Firestore docs that pre-date the field keep loading.  Missing
    individual keys fall back to 1.0 (no markup) -- the safe default.
    """
    sub = block.get("priority_cost_multipliers")
    if not isinstance(sub, dict):
        return {"low": 1.0, "normal": 1.1, "high": 1.2}
    sub_ctx = f"{ctx}.priority_cost_multipliers"
    out: dict[str, float] = {}
    for key, fallback in (("low", 1.0), ("normal", 1.1), ("high", 1.2)):
        if key in sub:
            try:
                out[key] = float(sub[key])
                continue
            except (TypeError, ValueError) as exc:
                raise FleetException(
                    f"config.json {sub_ctx}.{key} is not a valid number: "
                    f"{sub[key]!r}",
                ) from exc
        out[key] = fallback
    return out


@dataclass(frozen=True)
class FrameAllocationConfig:
    """Aggregate of every knob the frame-allocator turns: scoring
    weights, per-fleet startup buffer, per-fleet failure-rate widening,
    per-fleet VRAM boost, and the render-time calibration block.  All
    sourced from the ``frame_allocation`` block in config.json so
    tuning happens in one place.
    """
    weights: "AllocationWeights"
    vram_fleet_boost: VramFleetBoostConfig
    startup_buffer: StartupBufferConfig
    failure_rate: FailureRateConfig
    render_time: RenderTimeConfig

    @classmethod
    def from_env(cls) -> FrameAllocationConfig:
        return cls(
            weights=_load_allocation_weights(),
            vram_fleet_boost=VramFleetBoostConfig.from_env(),
            startup_buffer=StartupBufferConfig.from_env(),
            failure_rate=FailureRateConfig.from_env(),
            render_time=RenderTimeConfig.from_env(),
        )


@dataclass(frozen=True)
class BillingConfig:
    """User-credit accounting tunables.  ``credits_per_usd`` is the
    abstract-token rate: every $1 of ``actual_cost_usd`` debits this
    many credits from the user's Firestore balance.  Decouples display
    units from USD so promo / bonus rates can change without touching
    the cost path.
    """
    credits_per_usd: float = 100.0

    @classmethod
    def from_env(cls) -> "BillingConfig":
        block = _require_block("billing")
        return cls(
            credits_per_usd=_require_field_float(block, "billing", "credits_per_usd"),
        )


@dataclass(frozen=True)
class AppConfig:
    vast: VastConfig
    modal: ModalConfig
    public_backend_url: str
    frame_allocation: FrameAllocationConfig
    billing: BillingConfig
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

    @classmethod
    def from_env(cls) -> AppConfig:
        vast = VastConfig.from_env()
        modal = ModalConfig.from_env()
        community_block = _require_block("community")
        return cls(
            vast=vast,
            modal=modal,
            public_backend_url=_env_str("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            frame_allocation=FrameAllocationConfig.from_env(),
            billing=BillingConfig.from_env(),
            community_price_per_hour=_load_community_price_per_hour(),
            community_dispatch_claim_timeout_sec=_require_field_int(
                community_block, "community", "dispatch_claim_timeout_sec",
            ),
            orphan_secret=_env_str("ORPHAN_SECRET", ""),
            stall=StallDetectionConfig.from_env(),
        )
