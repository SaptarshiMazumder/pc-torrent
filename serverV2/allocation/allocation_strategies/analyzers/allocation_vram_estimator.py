"""VramEstimator -- predicts the VRAM working set of a scene.

Pure module.  Returns a single float (GB) the planner uses to filter
out GPUs that can't hold the scene.  Inputs are the same heaviness
fields the time analyzer reads -- one analyser pass, two consumers.

The estimate is conservative-but-not-paranoid: every dominant memory
consumer in Cycles/EEVEE is modelled, but with linear-ish growth
factors rather than worst-case constants.  Engine kernels + framebuffer
+ working textures + BVH + caches all add up; we bake them in.

Composition::

    base                                       (engine + buffers + safety)
    + vertex_factor(vertex_count_total)        (BVH + geometry)
    + texture_factor(texture_total_bytes)      (texture VRAM)
    + volumetrics_term(uses_volumetrics)       (3D grids)
    + particles_term(uses_particles)           (particle caches)
    + geometry_nodes_term(geometry_nodes_complexity)
    + pixel_factor(effective_pixels)           (framebuffer + AOVs)

All inputs come from the heaviness dict produced by
``parse_analysis_heaviness``; missing or zero values just fall through
to their no-op contribution.

Smoke test: ``python -m serverV2.allocation.allocation_strategies.analyzers.allocation_vram_estimator``
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------
# Tunables -- conservative but not paranoid.  All values in GB.
# ---------------------------------------------------------------------

# Engine + framebuffer + working textures + headroom.  Cycles/OPTIX
# typically eats ~1.5-2 GB before the scene loads.
BASE_VRAM_GB = 2.0

# Per-frame framebuffer + AOVs.  1080p baseline ~0.5 GB; scales linearly
# with pixel count (4K is ~2 GB just for buffers).
BASELINE_PIXELS = 1920 * 1080
PIXELS_VRAM_GB_AT_BASELINE = 0.5

# Vertex BVH + geometry.  Rough: 100M verts ~6 GB after BVH + tangents.
VERTS_GB_PER_MILLION = 0.06     # 100M verts -> 6 GB

# Texture VRAM.  Assume ~1.5x compressed-on-disk size after VRAM upload
# (mipmaps + decompression).  Heaviness reports bytes-on-disk.
TEXTURE_VRAM_MULTIPLIER = 1.5

# Volumetrics: large 3D grids, hard to predict without resolution -- bake
# in a flat term that's deliberately on the high side.
VOLUMETRICS_TERM_GB = 4.0

# Particles + caches: likewise hard to bound.
PARTICLES_TERM_GB = 2.0

# Geometry nodes blow up procedurally.  Capped at 8 GB so the estimate
# doesn't run away on highly-procedural scenes.
GN_GB_PER_COMPLEXITY = 0.04
GN_MAX_GB = 8.0

# Floor on the final estimate so we never claim a scene fits in <2 GB.
MIN_VRAM_GB = 2.0


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def estimate_required_vram_gb(heaviness: dict[str, Any] | None) -> float:
    """Estimate VRAM working-set in GB for the scene described by
    ``heaviness``.

    ``heaviness`` is the dict produced by ``parse_analysis_heaviness``;
    every key is defaulted there so we don't None-check here.  ``None``
    falls through to the BASE term -- treated as a tiny / unknown scene.
    """
    if heaviness is None:
        return BASE_VRAM_GB

    total = BASE_VRAM_GB
    total += _vertex_term(int(heaviness.get("vertex_count_total", 0) or 0))
    total += _texture_term(int(heaviness.get("texture_total_bytes", 0) or 0))
    total += _pixel_term(int(heaviness.get("effective_pixels", 0) or 0))
    if bool(heaviness.get("uses_volumetrics", False)):
        total += VOLUMETRICS_TERM_GB
    if bool(heaviness.get("uses_particles", False)):
        total += PARTICLES_TERM_GB
    if bool(heaviness.get("uses_geometry_nodes", False)):
        total += _geometry_nodes_term(
            int(heaviness.get("geometry_nodes_complexity", 0) or 0),
        )

    return max(MIN_VRAM_GB, total)


# ---------------------------------------------------------------------
# Per-factor functions -- pure, side-effect free.
# ---------------------------------------------------------------------

def _vertex_term(vertex_count: int) -> float:
    if vertex_count <= 0:
        return 0.0
    return (vertex_count / 1_000_000) * VERTS_GB_PER_MILLION


def _texture_term(texture_bytes: int) -> float:
    if texture_bytes <= 0:
        return 0.0
    gb_on_disk = texture_bytes / (1024 ** 3)
    return gb_on_disk * TEXTURE_VRAM_MULTIPLIER


def _pixel_term(effective_pixels: int) -> float:
    if effective_pixels <= 0:
        return 0.0
    return (effective_pixels / BASELINE_PIXELS) * PIXELS_VRAM_GB_AT_BASELINE


def _geometry_nodes_term(complexity: int) -> float:
    if complexity <= 0:
        return 0.0
    return min(GN_MAX_GB, complexity * GN_GB_PER_COMPLEXITY)


# ---------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------

def _smoke() -> None:
    print("=== VramEstimator smoke test ===\n")
    from serverV2.core.value_objects import parse_analysis_heaviness

    # ----- Empty / unknown heaviness -> base + default-pixel term -----
    # parse_analysis_heaviness(None) defaults effective_pixels to 1080p,
    # so the "empty" estimate is base + the 1080p framebuffer term.
    base = estimate_required_vram_gb(parse_analysis_heaviness(None))
    assert BASE_VRAM_GB <= base <= BASE_VRAM_GB + 1.0, f"empty -> base+pixel, got {base}"
    print(f"empty heaviness (1080p default)      -> {base:5.2f} GB")

    # ----- 1080p empty scene + small file -----
    light = parse_analysis_heaviness(None)
    light["effective_pixels"] = 1920 * 1080
    spf = estimate_required_vram_gb(light)
    assert 2.0 <= spf <= 3.0, f"1080p empty should be ~2.5 GB, got {spf}"
    print(f"1080p empty                          -> {spf:5.2f} GB")

    # ----- 10M verts -> base 2.0 + 1080p 0.5 + verts 0.6 = ~3.1 -----
    geo = parse_analysis_heaviness(None)
    geo["vertex_count_total"] = 10_000_000
    spf = estimate_required_vram_gb(geo)
    assert 2.8 <= spf <= 3.4, f"10M verts -> ~3.1 GB, got {spf}"
    print(f"10M verts                            -> {spf:5.2f} GB")

    # ----- 1 GB textures -> base 2.0 + 1080p 0.5 + tex 1.5 = ~4.0 -----
    tex = parse_analysis_heaviness(None)
    tex["texture_total_bytes"] = 1024 ** 3
    spf = estimate_required_vram_gb(tex)
    assert 3.5 <= spf <= 4.5, f"1 GB tex -> ~4.0 GB, got {spf}"
    print(f"1 GB textures                        -> {spf:5.2f} GB")

    # ----- Volumetrics -> base 2.0 + 1080p 0.5 + vol 4.0 = ~6.5 -----
    vol = parse_analysis_heaviness(None)
    vol["uses_volumetrics"] = True
    spf = estimate_required_vram_gb(vol)
    assert 6.0 <= spf <= 7.0, f"vol -> ~6.5 GB, got {spf}"
    print(f"+volumetrics                         -> {spf:5.2f} GB")

    # ----- Heavy scene -- 50M verts, 4 GB tex, vol, particles, GN -----
    heavy = parse_analysis_heaviness(None)
    heavy["vertex_count_total"] = 50_000_000
    heavy["texture_total_bytes"] = 4 * 1024 ** 3
    heavy["effective_pixels"] = 3840 * 2160
    heavy["uses_volumetrics"] = True
    heavy["uses_particles"] = True
    heavy["uses_geometry_nodes"] = True
    heavy["geometry_nodes_complexity"] = 100
    spf = estimate_required_vram_gb(heavy)
    # base 2 + verts 3 + tex 6 + 4K pixels 2 + vol 4 + parts 2 + gn 4 = ~23 GB
    assert 20 <= spf <= 26, f"heavy scene -> ~23 GB, got {spf}"
    print(f"heavy (50M v, 4GB tex, 4K, vol+gn)   -> {spf:5.2f} GB")

    # ----- Pathological geometry-nodes scene -- capped at GN_MAX_GB -----
    # base 2 + 1080p 0.5 + GN cap 8 = ~10.5
    gn_runaway = parse_analysis_heaviness(None)
    gn_runaway["uses_geometry_nodes"] = True
    gn_runaway["geometry_nodes_complexity"] = 100_000
    spf = estimate_required_vram_gb(gn_runaway)
    assert spf <= BASE_VRAM_GB + PIXELS_VRAM_GB_AT_BASELINE + GN_MAX_GB + 0.1, \
        f"GN should cap at {GN_MAX_GB} GB extra, got {spf}"
    print(f"GN complexity=100k (capped)          -> {spf:5.2f} GB")

    # ----- Monotonicity -- heavier always >= lighter -----
    light = parse_analysis_heaviness(None)
    medium = parse_analysis_heaviness(None)
    medium["vertex_count_total"] = 5_000_000
    heavy = parse_analysis_heaviness(None)
    heavy["vertex_count_total"] = 50_000_000
    heavy["uses_volumetrics"] = True
    assert (
        estimate_required_vram_gb(light)
        < estimate_required_vram_gb(medium)
        < estimate_required_vram_gb(heavy)
    )
    print(f"\nmonotonicity: light < medium < heavy   OK")

    print("\nAll smoke tests passed.")


if __name__ == "__main__":
    _smoke()
