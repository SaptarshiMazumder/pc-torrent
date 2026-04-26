"""TimeAnalyzer — heuristic seconds-per-frame estimator.

Pure module.  Given a heaviness snapshot (from
``parse_analysis_heaviness``) and a target's ``render_speed``, returns
an estimated seconds-per-frame value.  Used by the cost analyzer and
the cost-aware allocation strategies (Phases 4-7 of
tiered_allocation_plan.md).

The output is a *ranking signal*, not a wall-time prediction.  All
factor formulas are tunable constants near the top of the file.  Once
Phase 5 telemetry accumulates, fit ``BASELINE_SEC`` and the factor
functions to real seconds-per-frame data.

Composition::

    multiplier =
        sample_factor(samples, engine)
      * pixel_factor(effective_pixels)
      * geometry_factor(vertex_count_total)
      * texture_factor(texture_total_bytes)
      * shader_factor(shader_node_count_total, uses_sss, uses_volumetrics)
      * feature_factor(subdivision, displacement, particles,
                       geometry_nodes, gn_complexity)

    seconds_per_frame = BASELINE_SEC * multiplier / max(MIN_RENDER_SPEED, render_speed)

Smoke test: ``python -m serverV2.orchestrator.allocation.analyzers.time_analyzer``
"""

from __future__ import annotations

import math
from typing import Any

from serverV2.core.value_objects import parse_analysis_heaviness


# ---------------------------------------------------------------------------
# Tunables — all factors are 1.0 for the baseline scene.
# ---------------------------------------------------------------------------

# Baseline scene: CYCLES, 1024 samples, 1080p, ~baseline geometry, simple
# shaders, no heavy features, render_speed=1.0.
BASELINE_SEC = 30.0

# Reference values for "1.0x factor"
BASELINE_PIXELS = 1920 * 1080            # 1080p
BASELINE_VERTS = 100_000                 # moderate scene
BASELINE_TEX_BYTES = 256 * 1024 * 1024   # 256 MB of textures (estimated VRAM cost)

# Sample baselines (engine-specific because EEVEE TAA samples are way cheaper
# per-sample than Cycles path samples).
CYCLES_BASELINE_SAMPLES = 1024
EEVEE_BASELINE_SAMPLES = 64

# Heavy-feature multipliers (each compounds independently)
SUBDIVISION_FACTOR = 1.3
DISPLACEMENT_FACTOR = 1.3
PARTICLES_FACTOR = 1.5
SUBSURFACE_FACTOR = 1.4
VOLUMETRICS_FACTOR = 2.0

# Geometry-nodes — base penalty + complexity-scaled, capped
GEOMETRY_NODES_BASE = 1.2
GEOMETRY_NODES_CAP = 0.8                  # +0.8 max from complexity
GEOMETRY_NODES_PER_NODE = 0.8 / 200.0     # complexity 200 → at the cap

# Shader complexity — flat below threshold, ramps to +100% at the upper threshold
SHADER_NODE_BASE_THRESHOLD = 100
SHADER_NODE_CAP_THRESHOLD = 600
SHADER_NODE_RAMP = 1.0 / (SHADER_NODE_CAP_THRESHOLD - SHADER_NODE_BASE_THRESHOLD)

# Texture VRAM-pressure proxy — mild penalty above baseline tex bytes
TEX_PRESSURE_RAMP_RATE = 0.3
TEX_PRESSURE_CAP = 2.0

# Floors — guard against pathological inputs producing absurd estimates
MIN_PIXEL_FACTOR = 0.25
MIN_GEOMETRY_FACTOR = 0.7
MIN_SAMPLE_FACTOR_EEVEE = 0.5
MIN_RENDER_SPEED = 0.1                    # also guards against /0


# ---------------------------------------------------------------------------
# Per-chunk startup overhead — fixed cost paid once per chunk regardless of
# how many frames it owns.  Heavy scenes can spend 5-20 minutes here.
#
#   startup_sec = BASELINE_STARTUP_SEC
#               + file_size_bytes / GB        * DOWNLOAD_SEC_PER_GB
#               + vertex_count_total / 1M     * BVH_SEC_PER_MILLION_VERTS
#               + texture_total_bytes / GB    * TEX_UPLOAD_SEC_PER_GB
#               + SHADER_COMPILE_SEC_BASE
#               + shader_node_count_total     * SHADER_COMPILE_SEC_PER_NODE
#   clamped to [BASELINE_STARTUP_SEC, MAX_STARTUP_SEC]
# ---------------------------------------------------------------------------

BASELINE_STARTUP_SEC = 90.0          # container boot + Blender start + small fixed costs
DOWNLOAD_SEC_PER_GB = 30.0           # R2-to-worker bandwidth (~33 MB/s sustained)
BVH_SEC_PER_MILLION_VERTS = 3.0      # Cycles geometry pre-process
TEX_UPLOAD_SEC_PER_GB = 100.0        # VRAM upload + mipmap + decompression
SHADER_COMPILE_SEC_BASE = 5.0        # one-time OptiX/EEVEE kernel compile cost
SHADER_COMPILE_SEC_PER_NODE = 0.05   # heavy-shader-graph multiplier
MAX_STARTUP_SEC = 30 * 60            # 30-minute cap — runaway guard

_BYTES_PER_GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Factor functions — pure, side-effect free.  Each returns 1.0 for the
# baseline scene; > 1.0 for heavier-than-baseline; < 1.0 for lighter.
# ---------------------------------------------------------------------------

def _sample_factor(samples: int, engine: str) -> float:
    """Linear in samples for Cycles; EEVEE TAA ratio'd to a smaller baseline."""
    if samples <= 0:
        return 1.0
    if engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        return max(MIN_SAMPLE_FACTOR_EEVEE, samples / EEVEE_BASELINE_SAMPLES)
    return samples / CYCLES_BASELINE_SAMPLES


def _pixel_factor(effective_pixels: int) -> float:
    if effective_pixels <= 0:
        return 1.0
    return max(MIN_PIXEL_FACTOR, effective_pixels / BASELINE_PIXELS)


def _geometry_factor(vertex_count: int) -> float:
    """Sub-linear (log10): 100k → 1.0, 1M → ~1.5, 10M → ~2.0.

    Geometry traversal isn't linear in vertex count (BVH lookup is sub-linear),
    so we don't penalize big scenes proportionally.
    """
    if vertex_count <= 0:
        return 1.0
    ratio = vertex_count / BASELINE_VERTS
    return max(MIN_GEOMETRY_FACTOR, 1.0 + 0.5 * math.log10(max(0.1, ratio)))


def _texture_factor(tex_bytes: int) -> float:
    """Mild penalty above baseline tex bytes — proxy for VRAM pressure.
    Returns 1.0 below baseline; above, ramps slowly toward the cap.
    """
    if tex_bytes <= 0:
        return 1.0
    ratio = tex_bytes / BASELINE_TEX_BYTES
    if ratio <= 1.0:
        return 1.0
    return min(TEX_PRESSURE_CAP, 1.0 + TEX_PRESSURE_RAMP_RATE * (ratio - 1.0))


def _shader_factor(node_count: int, uses_sss: bool, uses_volumetrics: bool) -> float:
    f = 1.0
    if node_count > SHADER_NODE_BASE_THRESHOLD:
        excess = min(
            node_count - SHADER_NODE_BASE_THRESHOLD,
            SHADER_NODE_CAP_THRESHOLD - SHADER_NODE_BASE_THRESHOLD,
        )
        f *= 1.0 + excess * SHADER_NODE_RAMP
    if uses_sss:
        f *= SUBSURFACE_FACTOR
    if uses_volumetrics:
        f *= VOLUMETRICS_FACTOR
    return f


def _feature_factor(
    uses_subdivision: bool,
    uses_displacement: bool,
    uses_particles: bool,
    uses_geometry_nodes: bool,
    gn_complexity: int,
) -> float:
    f = 1.0
    if uses_subdivision:
        f *= SUBDIVISION_FACTOR
    if uses_displacement:
        f *= DISPLACEMENT_FACTOR
    if uses_particles:
        f *= PARTICLES_FACTOR
    if uses_geometry_nodes:
        gn_extra = min(GEOMETRY_NODES_CAP, gn_complexity * GEOMETRY_NODES_PER_NODE)
        f *= GEOMETRY_NODES_BASE + gn_extra
    return f


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_seconds_per_frame(
    heaviness: dict[str, Any],
    render_speed: float,
) -> float:
    """Heuristic seconds-per-frame estimate for one target.

    ``heaviness`` is the already-defaulted dict from
    ``parse_analysis_heaviness`` — every key has a sensible value so
    callers never need to None-check.

    ``render_speed`` is the target's relative speed (1.0 = baseline).
    Pulled from FleetCapability or CommunityMachine.

    Returns seconds-per-frame as a float.  Output is a ranking signal,
    not a wall-time prediction — see module docstring.
    """
    engine = str(heaviness.get("render_engine") or "")

    multiplier = (
        _sample_factor(heaviness.get("samples", 0), engine)
        * _pixel_factor(heaviness.get("effective_pixels", 0))
        * _geometry_factor(heaviness.get("vertex_count_total", 0))
        * _texture_factor(heaviness.get("texture_total_bytes", 0))
        * _shader_factor(
            heaviness.get("shader_node_count_total", 0),
            bool(heaviness.get("uses_subsurface_scattering", False)),
            bool(heaviness.get("uses_volumetrics", False)),
        )
        * _feature_factor(
            bool(heaviness.get("uses_subdivision", False)),
            bool(heaviness.get("uses_displacement", False)),
            bool(heaviness.get("uses_particles", False)),
            bool(heaviness.get("uses_geometry_nodes", False)),
            int(heaviness.get("geometry_nodes_complexity", 0)),
        )
    )

    return BASELINE_SEC * multiplier / max(MIN_RENDER_SPEED, render_speed)


def estimate_seconds_per_frame_from_snapshot(
    analysis_snapshot: dict[str, Any] | None,
    render_speed: float,
) -> float:
    """Convenience wrapper — pulls the heaviness sub-dict from the raw
    analysis snapshot, then delegates to ``estimate_seconds_per_frame``.

    Use this entry point when you have the raw snapshot from the DB or
    API; use ``estimate_seconds_per_frame`` when you've already parsed
    the heaviness once and are calling for many targets.
    """
    heaviness = parse_analysis_heaviness(analysis_snapshot)
    return estimate_seconds_per_frame(heaviness, render_speed)


def estimate_startup_seconds(
    heaviness: dict[str, Any],
    file_size_bytes: int = 0,
) -> float:
    """Per-chunk startup overhead in seconds.

    Includes container boot, Blender start, blend-file download, BVH
    build, texture VRAM upload, and shader compile.  Paid once per
    chunk regardless of how many frames the chunk owns — the cost
    analyzer applies it once per ``MixSlot``.

    Heavy scenes commonly land in the 5-20 minute range here.  The
    constants are coarse heuristics; Phase 5 telemetry will refine.
    """
    startup = BASELINE_STARTUP_SEC

    # File download from R2 to the worker.  Takes the full file size,
    # which already includes packed textures.
    if file_size_bytes > 0:
        startup += (file_size_bytes / _BYTES_PER_GB) * DOWNLOAD_SEC_PER_GB

    verts = int(heaviness.get("vertex_count_total", 0) or 0)
    if verts > 0:
        startup += (verts / 1_000_000) * BVH_SEC_PER_MILLION_VERTS

    tex_bytes = int(heaviness.get("texture_total_bytes", 0) or 0)
    if tex_bytes > 0:
        startup += (tex_bytes / _BYTES_PER_GB) * TEX_UPLOAD_SEC_PER_GB

    nodes = int(heaviness.get("shader_node_count_total", 0) or 0)
    materials = int(heaviness.get("material_count", 0) or 0)
    if materials > 0 or nodes > 0:
        startup += SHADER_COMPILE_SEC_BASE + nodes * SHADER_COMPILE_SEC_PER_NODE

    return min(MAX_STARTUP_SEC, startup)


def estimate_startup_seconds_from_snapshot(
    analysis_snapshot: dict[str, Any] | None,
    file_size_bytes: int = 0,
) -> float:
    """Convenience wrapper — pulls heaviness from snapshot, then
    delegates to ``estimate_startup_seconds``."""
    heaviness = parse_analysis_heaviness(analysis_snapshot)
    return estimate_startup_seconds(heaviness, file_size_bytes)


# ---------------------------------------------------------------------------
# Smoke test — `python -m serverV2.orchestrator.allocation.analyzers.time_analyzer`
# Hard-asserts that the heuristic ranks scenes plausibly.  Catches regressions
# in the factor functions or constants.
# ---------------------------------------------------------------------------

def _smoke() -> None:
    print("=== TimeAnalyzer smoke test ===\n")

    # ------- Baseline: empty heaviness, default render_speed -------
    # parse_analysis_heaviness(None) gives all defaults — every factor returns
    # 1.0, so we expect exactly BASELINE_SEC at speed=1.0.
    baseline = parse_analysis_heaviness(None)
    spf = estimate_seconds_per_frame(baseline, 1.0)
    assert abs(spf - BASELINE_SEC) < 0.5, f"baseline != {BASELINE_SEC}: {spf}"
    print(f"baseline (defaults)                       @ speed=1.0  ->{spf:.1f}s/frame")

    # ------- Faster GPU should reduce time -------
    fast_spf = estimate_seconds_per_frame(baseline, 2.0)
    assert fast_spf < spf, f"speed=2.0 should be faster than speed=1.0"
    assert abs(fast_spf - BASELINE_SEC / 2.0) < 0.5
    print(f"baseline                                  @ speed=2.0  ->{fast_spf:.1f}s/frame")

    # ------- Cycles 4x samples → ~4x time (linear) -------
    heavy_samples = dict(baseline)
    heavy_samples["render_engine"] = "CYCLES"
    heavy_samples["samples"] = 4 * CYCLES_BASELINE_SAMPLES   # 4096
    spf_samples = estimate_seconds_per_frame(heavy_samples, 1.0)
    assert 3.5 * BASELINE_SEC < spf_samples < 4.5 * BASELINE_SEC, \
        f"4x samples should ~4x time, got {spf_samples}"
    print(f"4x CYCLES samples                         @ speed=1.0  ->{spf_samples:.1f}s/frame")

    # ------- EEVEE 64 TAA = baseline -------
    eevee = dict(baseline)
    eevee["render_engine"] = "BLENDER_EEVEE"
    eevee["samples"] = EEVEE_BASELINE_SAMPLES   # 64
    eevee_spf = estimate_seconds_per_frame(eevee, 1.0)
    assert abs(eevee_spf - BASELINE_SEC) < 0.5, f"EEVEE 64 TAA != baseline: {eevee_spf}"
    print(f"EEVEE 64 TAA samples                      @ speed=1.0  ->{eevee_spf:.1f}s/frame")

    # ------- 4K resolution → ~4x time -------
    heavy_res = dict(baseline)
    heavy_res["effective_pixels"] = BASELINE_PIXELS * 4
    spf_4k = estimate_seconds_per_frame(heavy_res, 1.0)
    assert 3.5 * BASELINE_SEC < spf_4k < 4.5 * BASELINE_SEC, \
        f"4x pixels should ~4x time, got {spf_4k}"
    print(f"4K resolution (4x pixels)                 @ speed=1.0  ->{spf_4k:.1f}s/frame")

    # ------- 10M verts (100x baseline) — log → ~2x -------
    heavy_geom = dict(baseline)
    heavy_geom["vertex_count_total"] = BASELINE_VERTS * 100
    spf_geom = estimate_seconds_per_frame(heavy_geom, 1.0)
    assert spf_geom > BASELINE_SEC, "more geometry should increase time"
    assert spf_geom < 3 * BASELINE_SEC, "geometry factor should be sub-linear"
    print(f"10M verts (100x baseline)                 @ speed=1.0  ->{spf_geom:.1f}s/frame")

    # ------- Volumetrics → ~2x -------
    heavy_vol = dict(baseline)
    heavy_vol["uses_volumetrics"] = True
    spf_vol = estimate_seconds_per_frame(heavy_vol, 1.0)
    assert 1.8 * BASELINE_SEC < spf_vol < 2.2 * BASELINE_SEC, \
        f"volumetrics should ~2x, got {spf_vol}"
    print(f"+ volumetrics                             @ speed=1.0  ->{spf_vol:.1f}s/frame")

    # ------- Subsurface scattering → ~1.4x -------
    heavy_sss = dict(baseline)
    heavy_sss["uses_subsurface_scattering"] = True
    spf_sss = estimate_seconds_per_frame(heavy_sss, 1.0)
    assert 1.3 * BASELINE_SEC < spf_sss < 1.5 * BASELINE_SEC, \
        f"SSS should ~1.4x, got {spf_sss}"
    print(f"+ subsurface scattering                   @ speed=1.0  ->{spf_sss:.1f}s/frame")

    # ------- Subdivision → 1.3x -------
    heavy_subdiv = dict(baseline)
    heavy_subdiv["uses_subdivision"] = True
    spf_subdiv = estimate_seconds_per_frame(heavy_subdiv, 1.0)
    assert 1.25 * BASELINE_SEC < spf_subdiv < 1.35 * BASELINE_SEC
    print(f"+ subdivision modifier                    @ speed=1.0  ->{spf_subdiv:.1f}s/frame")

    # ------- Geometry nodes (high complexity) — base 1.2 + capped extra -------
    heavy_gnodes = dict(baseline)
    heavy_gnodes["uses_geometry_nodes"] = True
    heavy_gnodes["geometry_nodes_complexity"] = 200    # at the cap
    spf_gn = estimate_seconds_per_frame(heavy_gnodes, 1.0)
    assert 1.95 * BASELINE_SEC < spf_gn < 2.05 * BASELINE_SEC, \
        f"GN at cap should ~2x (1.2 + 0.8), got {spf_gn}"
    print(f"+ geometry nodes (complexity 200, capped) @ speed=1.0  ->{spf_gn:.1f}s/frame")

    # ------- Combined heavy scene — multiplicative -------
    heavy_all = dict(baseline)
    heavy_all["render_engine"] = "CYCLES"
    heavy_all["samples"] = 2048                     # 2x
    heavy_all["effective_pixels"] = BASELINE_PIXELS * 4   # 4x
    heavy_all["uses_subdivision"] = True
    heavy_all["uses_displacement"] = True
    heavy_all["uses_volumetrics"] = True
    heavy_all["uses_subsurface_scattering"] = True
    heavy_all["uses_geometry_nodes"] = True
    heavy_all["geometry_nodes_complexity"] = 100
    heavy_all["shader_node_count_total"] = 400
    spf_all = estimate_seconds_per_frame(heavy_all, 1.0)
    assert spf_all > 10 * BASELINE_SEC, "combined heaviness should >10x baseline"
    print(f"all-heavy (4K, 2x samples, all flags)     @ speed=1.0  ->{spf_all:.1f}s/frame")

    # ------- Legacy snapshot (no heaviness key) → BASELINE_SEC -------
    legacy_spf = estimate_seconds_per_frame_from_snapshot({"frame_start": 1, "frame_end": 10}, 1.0)
    assert abs(legacy_spf - BASELINE_SEC) < 0.5
    print(f"legacy snapshot (no heaviness key)        @ speed=1.0  ->{legacy_spf:.1f}s/frame")

    # ------- None snapshot — same as legacy -------
    none_spf = estimate_seconds_per_frame_from_snapshot(None, 1.0)
    assert abs(none_spf - BASELINE_SEC) < 0.5
    print(f"None snapshot                             @ speed=1.0  ->{none_spf:.1f}s/frame")

    # ------- Render-speed floor — pathologically slow target -------
    slow_spf = estimate_seconds_per_frame(baseline, 0.001)
    expected = BASELINE_SEC / MIN_RENDER_SPEED
    assert abs(slow_spf - expected) < 0.5, \
        f"speed=0.001 should clamp to {MIN_RENDER_SPEED}, expected {expected}, got {slow_spf}"
    print(f"speed=0.001 (clamped to {MIN_RENDER_SPEED})              ->{slow_spf:.1f}s/frame")

    # ------- Monotonicity: heavier scenes always rank higher than lighter -------
    light = dict(baseline)
    medium = dict(baseline); medium["uses_subdivision"] = True
    heavy = dict(baseline); heavy["uses_subdivision"] = True; heavy["uses_volumetrics"] = True
    assert (
        estimate_seconds_per_frame(light, 1.0)
        < estimate_seconds_per_frame(medium, 1.0)
        < estimate_seconds_per_frame(heavy, 1.0)
    ), "scenes should rank monotonically by heaviness"
    print(f"\nmonotonicity: light < medium < heavy           OK")

    # ------- Render-speed monotonicity: faster target always wins for same scene -------
    s_slow = estimate_seconds_per_frame(baseline, 0.5)
    s_baseline = estimate_seconds_per_frame(baseline, 1.0)
    s_fast = estimate_seconds_per_frame(baseline, 2.0)
    assert s_slow > s_baseline > s_fast, "speed should be inversely monotonic"
    print(f"monotonicity: slow > baseline > fast (speed)   OK")

    # ------- Startup estimator -------
    print("\n=== Startup overhead ===\n")

    # Empty heaviness + zero file → just baseline
    s_tiny = estimate_startup_seconds(parse_analysis_heaviness(None), 0)
    assert abs(s_tiny - BASELINE_STARTUP_SEC) < 1.0
    print(f"tiny scene (no file, no heaviness)        ->  {s_tiny:5.0f}s")

    # Moderate scene: 1 GB file, 10M verts, 500MB tex, 200 shader nodes
    moderate = parse_analysis_heaviness(None)
    moderate["vertex_count_total"] = 10_000_000
    moderate["texture_total_bytes"] = 512 * 1024 * 1024
    moderate["shader_node_count_total"] = 200
    moderate["material_count"] = 30
    s_moderate = estimate_startup_seconds(moderate, file_size_bytes=1 * 1024 ** 3)
    # 90 + 30 + 30 + 50 + 5 + 10 = 215s ≈ 3.5 min
    assert 200 < s_moderate < 280, f"moderate scene should be ~3-5 min, got {s_moderate}"
    print(f"moderate (1GB, 10M verts, 500MB tex)      ->  {s_moderate:5.0f}s  ({s_moderate/60:.1f} min)")

    # Heavy scene: 5 GB, 50M verts, 2GB tex, 500 nodes
    heavy = parse_analysis_heaviness(None)
    heavy["vertex_count_total"] = 50_000_000
    heavy["texture_total_bytes"] = 2 * 1024 ** 3
    heavy["shader_node_count_total"] = 500
    heavy["material_count"] = 80
    s_heavy = estimate_startup_seconds(heavy, file_size_bytes=5 * 1024 ** 3)
    # 90 + 150 + 150 + 200 + 5 + 25 = 620s ≈ 10 min
    assert 5 * 60 < s_heavy < 12 * 60, f"heavy scene should be 5-12 min, got {s_heavy}"
    print(f"heavy (5GB, 50M verts, 2GB tex)           ->  {s_heavy:5.0f}s  ({s_heavy/60:.1f} min)")

    # Very heavy scene: 10 GB, 100M verts, 4GB tex, 1000 nodes
    very_heavy = parse_analysis_heaviness(None)
    very_heavy["vertex_count_total"] = 100_000_000
    very_heavy["texture_total_bytes"] = 4 * 1024 ** 3
    very_heavy["shader_node_count_total"] = 1000
    very_heavy["material_count"] = 150
    s_very_heavy = estimate_startup_seconds(very_heavy, file_size_bytes=10 * 1024 ** 3)
    # 90 + 300 + 300 + 400 + 55 = 1145s ≈ 19 min
    assert 15 * 60 < s_very_heavy < 22 * 60, f"very-heavy should be 15-22 min, got {s_very_heavy}"
    print(f"very heavy (10GB, 100M verts, 4GB tex)    ->  {s_very_heavy:5.0f}s  ({s_very_heavy/60:.1f} min)")

    # Pathological scene → cap at MAX_STARTUP_SEC
    absurd = parse_analysis_heaviness(None)
    absurd["vertex_count_total"] = 10_000_000_000
    absurd["texture_total_bytes"] = 100 * 1024 ** 3
    absurd["shader_node_count_total"] = 100_000
    s_absurd = estimate_startup_seconds(absurd, file_size_bytes=100 * 1024 ** 3)
    assert s_absurd == MAX_STARTUP_SEC, f"runaway should cap at {MAX_STARTUP_SEC}, got {s_absurd}"
    print(f"absurd (capped to MAX_STARTUP_SEC)        ->  {s_absurd:5.0f}s  (capped)")

    # Startup monotonicity: heavier always >= lighter
    assert s_tiny < s_moderate < s_heavy < s_very_heavy <= s_absurd
    print(f"\nstartup monotonicity: tiny < moderate < heavy < very_heavy   OK")

    # Snapshot wrapper
    legacy_startup = estimate_startup_seconds_from_snapshot(None, 0)
    assert abs(legacy_startup - BASELINE_STARTUP_SEC) < 1.0
    print(f"legacy snapshot startup                          OK")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    _smoke()
