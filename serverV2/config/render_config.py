"""RenderConfig — typed mirror of ``serverV2/config.json``.

Storage shape and dataclass shape are identical: ``dataclasses.asdict``
of a ``RenderConfig`` equals the JSON blob in Firestore (and the bundled
``config.json``), and ``RenderConfig.from_dict`` parses that blob back.
This is the object the allocation planner reads fresh from the
``RenderConfigRepository`` at the start of each plan call, and the
object the admin endpoint round-trips through Firestore.

Reuses existing leaf dataclasses where their field names already match
the JSON inner keys (``AllocationWeights``, ``VramFleetBoostConfig``,
``StartupBufferConfig``, ``FailureRateConfig``, ``EngineFactors``,
``RenderStartupSec``).  New ``*Section`` dataclasses are defined here
for the parent containers where the existing AppConfig-side dataclass
field names diverged from JSON keys.

This is intentionally a separate hierarchy from ``AppConfig``.
``AppConfig`` continues to drive the legacy boot-time wiring (stall
watchdog, monitors, registry, etc.) from the bundled file.  ``Render
Config`` drives the planner from Firestore.  Both eat the same JSON
content; the typing differs because their use cases differ.
"""

from __future__ import annotations

from dataclasses import dataclass

from serverV2.config.allocation_weights import (
    AllocationWeights,
)
from serverV2.config.config import (
    EngineFactors,
    FailureRateConfig,
    RenderStartupSec,
    SceneScalingConfig,
    StartupBufferConfig,
    VramFleetBoostConfig,
)
from serverV2.fleets.fleet_exception import FleetException


# ---------------------------------------------------------------------------
# Top-level section dataclasses (one per top-level key in config.json).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModalSection:
    max_parallel: int
    per_gpu_max_parallel: int
    dispatch_timeout_sec: int
    in_queue_timeout_sec: int
    endpoint_url_prefix: str
    provisioning_enabled: bool
    availability_sec: float


@dataclass(frozen=True)
class VastSection:
    max_parallel: int
    disk_gb: int
    secure_cloud_only: bool
    poll_interval_sec: float
    startup_timeout_sec: float
    heartbeat_timeout_sec: float
    heartbeat_grace_sec: float
    provisioning_enabled: bool


@dataclass(frozen=True)
class OrchestratorSection:
    max_retries: int


@dataclass(frozen=True)
class CommunitySection:
    price_per_hour: float
    dispatch_claim_timeout_sec: int
    machine_stale_seconds: int
    machine_demote_seconds: int


@dataclass(frozen=True)
class BillingSection:
    credits_per_usd: float


@dataclass(frozen=True)
class MonitorSection:
    in_progress_stale_sec: int


@dataclass(frozen=True)
class DesktopSection:
    """Force-update gate for the desktop client.

    The /app/min-version endpoint hands these values to running
    desktops; if the desktop's bundled version is below ``min_version``
    it renders UpdateRequiredModal which deep-links to ``latest_url``
    for the user to download + install the new installer.

    Live-tunable via the ConfigurationPage admin UI -- bumping
    ``min_version`` after a new GitHub release force-blocks every
    older client on its next launch without a serverV2 redeploy.
    """
    min_version: str
    latest_url: str


@dataclass(frozen=True)
class StallLoadingSafetyConfig:
    """Per-heaviness coefficients the AllowedStallTimesResolver uses to
    compute its OWN expected-loading-time, independent of the cost-time
    analyzer's calibration.

    The cost analyzer's ``startup_sec`` is tuned for accuracy (estimate
    ~= actual cost paid).  This block is tuned for safety (window wide
    enough to let real loading finish).  Same shape -- file_size,
    verts, textures, shader nodes, per-fleet provisioning buffer --
    but separately tunable so future cost re-calibration can't silently
    shrink the stall window.
    """
    baseline_sec: float
    per_gb_file: float
    per_million_verts: float
    per_gb_texture: float
    per_shader_node: float
    fleet_buffer_vast: float
    fleet_buffer_modal: float
    fleet_buffer_community: float


@dataclass(frozen=True)
class StallSection:
    """Mirror of the ``stall`` block in config.json.  Field names match
    the JSON keys verbatim so ``asdict`` round-trips."""
    cpu_threshold_pct: float
    cpu_window_sec: float
    rss_noise_bytes: int
    download_bytes_stall_sec: float
    download_secs_per_gb: float
    download_phase_min_sec: float
    download_phase_max_sec: float
    loading_multiplier: float
    loading_phase_min_sec: float
    loading_phase_max_sec: float
    loading_safety: StallLoadingSafetyConfig
    hard_max_chunk_sec: float


@dataclass(frozen=True)
class RenderTimeSection:
    """Mirror of ``frame_allocation.render_time``.  ``startup_sec``
    field name matches the JSON key (the legacy ``RenderTimeConfig``
    uses ``startup`` -- different shape, separate type)."""
    baseline_sec_cycles: float
    baseline_sec_eevee: float
    factors_cycles: EngineFactors
    factors_eevee: EngineFactors
    scene_scaling_cycles: SceneScalingConfig
    scene_scaling_eevee: SceneScalingConfig
    startup_sec: RenderStartupSec


@dataclass(frozen=True)
class FrameAllocationSection:
    """Mirror of ``frame_allocation``.  Note ``startup_buffer_sec``
    field name (the legacy ``FrameAllocationConfig`` uses
    ``startup_buffer`` -- different name, separate type)."""
    weights: AllocationWeights
    vram_fleet_boost: VramFleetBoostConfig
    startup_buffer_sec: StartupBufferConfig
    failure_rate: FailureRateConfig
    render_time: RenderTimeSection


@dataclass(frozen=True)
class VastInstanceEntry:
    gpu_name: str
    label: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float


@dataclass(frozen=True)
class ModalInstanceEntry:
    gpu_type: str
    label: str
    vram_gb: float
    cpu_cores: int
    ram_gb: float
    render_speed: float
    price_per_hour: float


# ---------------------------------------------------------------------------
# Top-level RenderConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderConfig:
    modal: ModalSection
    vast: VastSection
    orchestrator: OrchestratorSection
    community: CommunitySection
    billing: BillingSection
    monitor: MonitorSection
    stall: StallSection
    frame_allocation: FrameAllocationSection
    desktop: DesktopSection
    vast_instances: tuple[VastInstanceEntry, ...]
    modal_instances: tuple[ModalInstanceEntry, ...]

    @classmethod
    def from_dict(cls, d: dict) -> "RenderConfig":
        return cls(
            modal=_modal(_block(d, "modal")),
            vast=_vast(_block(d, "vast")),
            orchestrator=_orchestrator(_block(d, "orchestrator")),
            community=_community(_block(d, "community")),
            billing=_billing(d.get("billing") or {}),
            monitor=_monitor(_block(d, "monitor")),
            stall=_stall(_block(d, "stall")),
            frame_allocation=_frame_allocation(_block(d, "frame_allocation")),
            desktop=_desktop(_block(d, "desktop")),
            vast_instances=tuple(
                _vast_instance(e, i)
                for i, e in enumerate(_list(d, "vast_instances"))
            ),
            modal_instances=tuple(
                _modal_instance(e, i)
                for i, e in enumerate(_list(d, "modal_instances"))
            ),
        )


# ---------------------------------------------------------------------------
# Parsing helpers — fail loud on missing or wrongly-typed fields.
# ---------------------------------------------------------------------------

def _block(d: dict, name: str) -> dict:
    block = d.get(name)
    if not isinstance(block, dict):
        raise FleetException(f"config missing required block: {name!r}")
    return block


def _list(d: dict, name: str) -> list:
    items = d.get(name)
    if not isinstance(items, list):
        raise FleetException(f"config missing required array: {name!r}")
    return items


def _str(block: dict, ctx: str, key: str) -> str:
    if key not in block:
        raise FleetException(f"{ctx} missing required key: {key}")
    return str(block[key])


def _bool(block: dict, ctx: str, key: str) -> bool:
    if key not in block:
        raise FleetException(f"{ctx} missing required key: {key}")
    return bool(block[key])


def _int(block: dict, ctx: str, key: str) -> int:
    if key not in block:
        raise FleetException(f"{ctx} missing required key: {key}")
    try:
        return int(block[key])
    except (TypeError, ValueError) as exc:
        raise FleetException(f"{ctx}.{key} not a valid int: {block[key]!r}") from exc


def _float(block: dict, ctx: str, key: str) -> float:
    if key not in block:
        raise FleetException(f"{ctx} missing required key: {key}")
    try:
        return float(block[key])
    except (TypeError, ValueError) as exc:
        raise FleetException(f"{ctx}.{key} not a valid number: {block[key]!r}") from exc


def _modal(b: dict) -> ModalSection:
    ctx = "modal"
    # ``availability_sec`` was added to the Firestore schema after this block
    # was already populated in production docs.  Fall back to the boot
    # default (14400) when absent so old docs keep parsing; the strict
    # parsers stay strict for every always-present field.
    availability_sec = (
        _float(b, ctx, "availability_sec") if "availability_sec" in b else 14400.0
    )
    return ModalSection(
        max_parallel=_int(b, ctx, "max_parallel"),
        per_gpu_max_parallel=_int(b, ctx, "per_gpu_max_parallel"),
        dispatch_timeout_sec=_int(b, ctx, "dispatch_timeout_sec"),
        in_queue_timeout_sec=_int(b, ctx, "in_queue_timeout_sec"),
        endpoint_url_prefix=_str(b, ctx, "endpoint_url_prefix"),
        provisioning_enabled=_bool(b, ctx, "provisioning_enabled"),
        availability_sec=availability_sec,
    )


def _vast(b: dict) -> VastSection:
    ctx = "vast"
    return VastSection(
        max_parallel=_int(b, ctx, "max_parallel"),
        disk_gb=_int(b, ctx, "disk_gb"),
        secure_cloud_only=_bool(b, ctx, "secure_cloud_only"),
        poll_interval_sec=_float(b, ctx, "poll_interval_sec"),
        startup_timeout_sec=_float(b, ctx, "startup_timeout_sec"),
        heartbeat_timeout_sec=_float(b, ctx, "heartbeat_timeout_sec"),
        heartbeat_grace_sec=_float(b, ctx, "heartbeat_grace_sec"),
        provisioning_enabled=_bool(b, ctx, "provisioning_enabled"),
    )


def _orchestrator(b: dict) -> OrchestratorSection:
    return OrchestratorSection(max_retries=_int(b, "orchestrator", "max_retries"))


def _community(b: dict) -> CommunitySection:
    ctx = "community"
    # ``machine_stale_seconds`` / ``machine_demote_seconds`` were added to the
    # schema after this block already existed in production docs (they used to
    # be hardcoded AppConfig defaults).  Fall back to those defaults when
    # absent so old docs keep parsing.
    machine_stale_seconds = (
        _int(b, ctx, "machine_stale_seconds") if "machine_stale_seconds" in b else 15
    )
    machine_demote_seconds = (
        _int(b, ctx, "machine_demote_seconds") if "machine_demote_seconds" in b else 90
    )
    return CommunitySection(
        price_per_hour=_float(b, ctx, "price_per_hour"),
        dispatch_claim_timeout_sec=_int(b, ctx, "dispatch_claim_timeout_sec"),
        machine_stale_seconds=machine_stale_seconds,
        machine_demote_seconds=machine_demote_seconds,
    )


def _billing(b: dict) -> BillingSection:
    ctx = "billing"
    # Whole ``billing`` block is optional on old docs; default matches the
    # former config.json value so behaviour is unchanged until re-saved.
    credits_per_usd = (
        _float(b, ctx, "credits_per_usd") if "credits_per_usd" in b else 100.0
    )
    return BillingSection(credits_per_usd=credits_per_usd)


def _monitor(b: dict) -> MonitorSection:
    return MonitorSection(in_progress_stale_sec=_int(b, "monitor", "in_progress_stale_sec"))


def _desktop(b: dict) -> DesktopSection:
    ctx = "desktop"
    return DesktopSection(
        min_version=_str(b, ctx, "min_version"),
        latest_url=_str(b, ctx, "latest_url"),
    )


def _stall(b: dict) -> StallSection:
    ctx = "stall"
    loading_safety_block = b.get("loading_safety")
    if not isinstance(loading_safety_block, dict):
        raise FleetException(f"{ctx}.loading_safety missing or not an object")
    return StallSection(
        cpu_threshold_pct=_float(b, ctx, "cpu_threshold_pct"),
        cpu_window_sec=_float(b, ctx, "cpu_window_sec"),
        rss_noise_bytes=_int(b, ctx, "rss_noise_bytes"),
        download_bytes_stall_sec=_float(b, ctx, "download_bytes_stall_sec"),
        download_secs_per_gb=_float(b, ctx, "download_secs_per_gb"),
        download_phase_min_sec=_float(b, ctx, "download_phase_min_sec"),
        download_phase_max_sec=_float(b, ctx, "download_phase_max_sec"),
        loading_multiplier=_float(b, ctx, "loading_multiplier"),
        loading_phase_min_sec=_float(b, ctx, "loading_phase_min_sec"),
        loading_phase_max_sec=_float(b, ctx, "loading_phase_max_sec"),
        loading_safety=_loading_safety(loading_safety_block),
        hard_max_chunk_sec=_float(b, ctx, "hard_max_chunk_sec"),
    )


def _loading_safety(b: dict) -> StallLoadingSafetyConfig:
    ctx = "stall.loading_safety"
    return StallLoadingSafetyConfig(
        baseline_sec=_float(b, ctx, "baseline_sec"),
        per_gb_file=_float(b, ctx, "per_gb_file"),
        per_million_verts=_float(b, ctx, "per_million_verts"),
        per_gb_texture=_float(b, ctx, "per_gb_texture"),
        per_shader_node=_float(b, ctx, "per_shader_node"),
        fleet_buffer_vast=_float(b, ctx, "fleet_buffer_vast"),
        fleet_buffer_modal=_float(b, ctx, "fleet_buffer_modal"),
        fleet_buffer_community=_float(b, ctx, "fleet_buffer_community"),
    )


def _per_fleet_block(b: dict, ctx: str) -> dict[str, float]:
    return {
        "vast": _float(b, ctx, "vast"),
        "modal": _float(b, ctx, "modal"),
        "community": _float(b, ctx, "community"),
    }


def _weights(b: dict) -> AllocationWeights:
    ctx = "frame_allocation.weights"
    # ``chunk_count_curve`` was added after this section was already
    # populated in production Firestore docs.  Fall back to the dataclass
    # default when the key is absent so old docs keep parsing; the strict
    # parsers stay strict for every other (always-present) field.
    chunk_count_curve = (
        _float(b, ctx, "chunk_count_curve") if "chunk_count_curve" in b else 1.0
    )
    # Phase 5 -- time-aware allocation knobs.  Same backwards-compat
    # pattern: missing on old docs falls back to dataclass defaults so
    # we don't fail to load against Firestore until every prod doc has
    # been re-saved through the ConfigurationPage UI.
    time_safety_factor = (
        _float(b, ctx, "time_safety_factor") if "time_safety_factor" in b else 1.5
    )
    time_headroom_falloff = (
        _float(b, ctx, "time_headroom_falloff") if "time_headroom_falloff" in b else 0.5
    )
    # Heavy-feature combination knobs -- backwards compatible with old
    # Firestore docs that pre-date the additive combination.
    secondary_feature_credit = (
        _float(b, ctx, "secondary_feature_credit")
        if "secondary_feature_credit" in b else 0.3
    )
    heavy_multiplier_cap = (
        _float(b, ctx, "heavy_multiplier_cap")
        if "heavy_multiplier_cap" in b else 5.0
    )
    # Per-priority cost multipliers.  Backwards-compatible with old
    # Firestore docs: any missing key falls back to (1.0, 1.1, 1.2).
    raw_pcm = b.get("priority_cost_multipliers")
    if isinstance(raw_pcm, dict):
        priority_cost_multipliers = {
            "low":    float(raw_pcm.get("low", 1.0)),
            "normal": float(raw_pcm.get("normal", 1.1)),
            "high":   float(raw_pcm.get("high", 1.2)),
        }
    else:
        priority_cost_multipliers = {"low": 1.0, "normal": 1.1, "high": 1.2}
    # Per-fleet target share of the serverless slots (replaces the old
    # symmetric ``fleet_diversification_cap``).  Backwards-compatible: old
    # docs missing the key fall back to the dataclass default (70/30).
    raw_share = b.get("fleet_target_share")
    if isinstance(raw_share, dict) and raw_share:
        fleet_target_share = {str(k): float(v) for k, v in raw_share.items()}
    else:
        fleet_target_share = {"modal_serverless": 0.70, "vast_serverless": 0.30}
    return AllocationWeights(
        speed_weight=_float(b, ctx, "speed_weight"),
        cuda_weight=_float(b, ctx, "cuda_weight"),
        os_weight=_float(b, ctx, "os_weight"),
        max_targets=_int(b, ctx, "max_targets"),
        min_frames_per_chunk=_int(b, ctx, "min_frames_per_chunk"),
        fleet_target_share=fleet_target_share,
        gpu_type_diversification_cap=_float(b, ctx, "gpu_type_diversification_cap"),
        vram_safety_factor=_float(b, ctx, "vram_safety_factor"),
        startup_amortization_ratio=_float(b, ctx, "startup_amortization_ratio"),
        chunk_count_curve=chunk_count_curve,
        time_safety_factor=time_safety_factor,
        time_headroom_falloff=time_headroom_falloff,
        secondary_feature_credit=secondary_feature_credit,
        heavy_multiplier_cap=heavy_multiplier_cap,
        priority_cost_multipliers=priority_cost_multipliers,
    )


def _engine_factors(b: dict, ctx: str) -> EngineFactors:
    return EngineFactors(
        subdivision=_float(b, ctx, "subdivision"),
        displacement=_float(b, ctx, "displacement"),
        particles=_float(b, ctx, "particles"),
        subsurface=_float(b, ctx, "subsurface"),
        volumetrics=_float(b, ctx, "volumetrics"),
        adaptive_sampling=_float(b, ctx, "adaptive_sampling"),
    )


def _startup_sec(b: dict) -> RenderStartupSec:
    ctx = "frame_allocation.render_time.startup_sec"
    return RenderStartupSec(
        baseline=_float(b, ctx, "baseline"),
        download_per_gb=_float(b, ctx, "download_per_gb"),
        bvh_per_million_verts=_float(b, ctx, "bvh_per_million_verts"),
        texture_upload_per_gb=_float(b, ctx, "texture_upload_per_gb"),
        shader_compile_base=_float(b, ctx, "shader_compile_base"),
        shader_compile_per_node=_float(b, ctx, "shader_compile_per_node"),
        max_total=_float(b, ctx, "max_total"),
    )


_SCENE_SCALING_CYCLES_DEFAULT = SceneScalingConfig(
    baseline_pixels=1920 * 1080,
    pixel_curve_exponent=0.85,
    min_pixel_factor=0.25,
    baseline_samples=1024.0,
    sample_curve_exponent=0.9,
    min_sample_factor=0.0,
)

_SCENE_SCALING_EEVEE_DEFAULT = SceneScalingConfig(
    baseline_pixels=1920 * 1080,
    pixel_curve_exponent=0.5,
    min_pixel_factor=0.25,
    baseline_samples=64.0,
    sample_curve_exponent=0.4,
    min_sample_factor=0.5,
)


def _scene_scaling(b: dict, key: str, defaults: SceneScalingConfig) -> SceneScalingConfig:
    """Backwards-compat at both granularities:
      * docs missing the whole section -> engine-appropriate defaults;
      * docs with a partial section (admin UI saves a half-filled form,
        seeds from older shape) -> per-field default fallback.

    The admin UI surfaces these fields lazily (they stay undefined in form
    state until touched), so a save without every field would otherwise
    400 the request.  Defaults match what ``allocation_time_analyzer``
    bakes in, so missing-field behaviour is identical to "section never
    written".
    """
    sub = b.get(key)
    if not isinstance(sub, dict):
        return defaults

    def _f(k: str, d: float) -> float:
        v = sub.get(k)
        if v is None:
            return d
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    return SceneScalingConfig(
        baseline_pixels=_f("baseline_pixels", defaults.baseline_pixels),
        pixel_curve_exponent=_f("pixel_curve_exponent", defaults.pixel_curve_exponent),
        min_pixel_factor=_f("min_pixel_factor", defaults.min_pixel_factor),
        baseline_samples=_f("baseline_samples", defaults.baseline_samples),
        sample_curve_exponent=_f("sample_curve_exponent", defaults.sample_curve_exponent),
        min_sample_factor=_f("min_sample_factor", defaults.min_sample_factor),
    )


def _render_time(b: dict) -> RenderTimeSection:
    ctx = "frame_allocation.render_time"
    factors_cycles_block = b.get("factors_cycles")
    factors_eevee_block = b.get("factors_eevee")
    startup_sec_block = b.get("startup_sec")
    if not isinstance(factors_cycles_block, dict):
        raise FleetException(f"{ctx}.factors_cycles missing or not an object")
    if not isinstance(factors_eevee_block, dict):
        raise FleetException(f"{ctx}.factors_eevee missing or not an object")
    if not isinstance(startup_sec_block, dict):
        raise FleetException(f"{ctx}.startup_sec missing or not an object")
    return RenderTimeSection(
        baseline_sec_cycles=_float(b, ctx, "baseline_sec_cycles"),
        baseline_sec_eevee=_float(b, ctx, "baseline_sec_eevee"),
        factors_cycles=_engine_factors(factors_cycles_block, f"{ctx}.factors_cycles"),
        factors_eevee=_engine_factors(factors_eevee_block, f"{ctx}.factors_eevee"),
        scene_scaling_cycles=_scene_scaling(b, "scene_scaling_cycles", _SCENE_SCALING_CYCLES_DEFAULT),
        scene_scaling_eevee=_scene_scaling(b, "scene_scaling_eevee", _SCENE_SCALING_EEVEE_DEFAULT),
        startup_sec=_startup_sec(startup_sec_block),
    )


def _frame_allocation(b: dict) -> FrameAllocationSection:
    weights_block = b.get("weights")
    vram_fleet_boost_block = b.get("vram_fleet_boost")
    startup_buffer_sec_block = b.get("startup_buffer_sec")
    failure_rate_block = b.get("failure_rate")
    render_time_block = b.get("render_time")
    for name, blk in [
        ("weights", weights_block),
        ("vram_fleet_boost", vram_fleet_boost_block),
        ("startup_buffer_sec", startup_buffer_sec_block),
        ("failure_rate", failure_rate_block),
        ("render_time", render_time_block),
    ]:
        if not isinstance(blk, dict):
            raise FleetException(f"frame_allocation.{name} missing or not an object")
    vfb = _per_fleet_block(vram_fleet_boost_block, "frame_allocation.vram_fleet_boost")
    sbs = _per_fleet_block(startup_buffer_sec_block, "frame_allocation.startup_buffer_sec")
    fr = _per_fleet_block(failure_rate_block, "frame_allocation.failure_rate")
    return FrameAllocationSection(
        weights=_weights(weights_block),
        vram_fleet_boost=VramFleetBoostConfig(**vfb),
        startup_buffer_sec=StartupBufferConfig(**sbs),
        failure_rate=FailureRateConfig(**fr),
        render_time=_render_time(render_time_block),
    )


def _vast_instance(e: dict, i: int) -> VastInstanceEntry:
    ctx = f"vast_instances[{i}]"
    if not isinstance(e, dict):
        raise FleetException(f"{ctx} not an object")
    return VastInstanceEntry(
        gpu_name=_str(e, ctx, "gpu_name"),
        label=_str(e, ctx, "label"),
        vram_gb=_float(e, ctx, "vram_gb"),
        cpu_cores=_int(e, ctx, "cpu_cores"),
        ram_gb=_float(e, ctx, "ram_gb"),
        render_speed=_float(e, ctx, "render_speed"),
    )


def _modal_instance(e: dict, i: int) -> ModalInstanceEntry:
    ctx = f"modal_instances[{i}]"
    if not isinstance(e, dict):
        raise FleetException(f"{ctx} not an object")
    return ModalInstanceEntry(
        gpu_type=_str(e, ctx, "gpu_type"),
        label=_str(e, ctx, "label"),
        vram_gb=_float(e, ctx, "vram_gb"),
        cpu_cores=_int(e, ctx, "cpu_cores"),
        ram_gb=_float(e, ctx, "ram_gb"),
        render_speed=_float(e, ctx, "render_speed"),
        price_per_hour=_float(e, ctx, "price_per_hour"),
    )
