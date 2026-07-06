"""CompositeScorer -- single-target ranking score (speed + CUDA + OS).

Pure module.  Given:

  * a target's specs (``render_speed``, optional ``cuda_version`` /
    ``host_os``, optional ``fleet`` for the per-fleet startup buffer)
  * the scene's heaviness (full dict from ``parse_analysis_heaviness``)
  * the current render's engine (so EEVEE-on-Windows can be penalised
    harshly even when speed/cuda look great)
  * an estimated chunk size (frames the target will own)
  * an ``AllocationWeights`` instance from the calling strategy

returns a single float -- higher = better.  The planner sorts the
eligible pool by this score to pick the mix.

Cost is an OPTIONAL scoring axis.  When ``weights.cost_weight`` > 0 a
``cost_factor`` is folded into the rank so the planner prefers
cost-effective hardware; at the default 0 the score is cost-blind.
``chunk_cost_for()`` also stays for telemetry (per-chunk USD estimate
stamped on every PlannedTask).

Composite shape::

    chunk_seconds = startup(heaviness, fleet) + spf(heaviness, render_speed) * frames
    speed_factor  = REF_SECONDS / max(chunk_seconds, 1.0)
    cuda_factor   = 0..1 from cuda_max_good (None = trust = 1.0)
    os_factor     = 0..1 from host_os + engine (None = trust = 1.0)
    cost_factor   = REF_COST / (REF_COST + chunk_cost)  in 0..1 (price <= 0 -> 1.0)
    score         = w.speed*speed_factor + w.cuda*cuda_factor + w.os*os_factor + w.cost*cost_factor

Speed dominates because its factor scales with chunk wall time (often
many multiples of 1.0 for fast chunks).  CUDA, OS and cost factors all
live in 0..1 and act as tiebreakers among similar-speed targets -- e.g.
two RTX 4090 offers with identical render_speed but a different driver
vintage, or a slightly cheaper offer of the same card.  Because cost is
bounded to 0..1 it nudges the ranking without ever burying speed, so a
dirt-cheap-but-weak card can't win on price alone.

The reference value normalises the speed factor so a baseline scene on
baseline hardware yields ~1.0.
"""

from __future__ import annotations

from typing import Any

from serverV2.config.allocation_weights import (
    AllocationWeights,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_time_analyzer import (
    estimate_seconds_per_frame,
    estimate_startup_seconds,
)


# 5 minutes of render = 300s.  A target completing a chunk in 300s
# yields speed_factor = 1.0; faster -> >1.0.
REF_SECONDS = 300.0

# Reference chunk cost (USD).  Sets the midpoint of the bounded 0..1 cost
# factor: a chunk costing $0.10 scores 0.5, cheaper -> toward 1.0, pricier
# -> toward 0.  Tune the midpoint here, tune the influence via
# weights.cost_weight.
REF_COST = 0.10

# Floor to avoid divide-by-zero on a zero-length chunk.
_MIN_CHUNK_SECONDS = 1.0

_EEVEE_ENGINES = frozenset({"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"})


# ---------------------------------------------------------------------
# Quality factors -- "None means trust"
# ---------------------------------------------------------------------
# Absence of a CUDA / OS signal means full credit (1.0), not neutral
# (0.5).  Rationale: penalties only fire when we have data showing the
# offer is bad.  Modal and community always carry None for both fields,
# so they score on speed alone -- no implicit demotion.

def cuda_factor(cuda_version: str | None) -> float:
    """0..1 score from a driver-reported max-CUDA string.

    12.0 -> 0.0  (old Hopper-era driver, OPTIX-failure-prone)
    13.0 -> 1.0  (current).
    None / unparseable -> 1.0 (trust the fleet).
    """
    if cuda_version is None:
        return 1.0
    try:
        v = float(cuda_version)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, v - 12.0))


def os_factor(host_os: str | None, engine: str | None) -> float:
    """0..1 score from host OS + engine.

    For EEVEE/EEVEE_NEXT renders: Windows hosts get 0.0 (broken EGL
    surface init in headless mode causes silent black-frame renders).
    Other engines: Windows gets 0.7 (soft penalty).  Linux always 1.0.
    None -> 1.0 (trust the fleet -- Modal/community).
    """
    if host_os is None:
        return 1.0
    s = host_os.lower()
    is_linux = "linux" in s or "ubuntu" in s or "debian" in s
    if engine in _EEVEE_ENGINES:
        return 1.0 if is_linux else 0.0
    return 1.0 if is_linux else 0.7


def cost_factor(chunk_seconds: float, price_per_hour: float) -> float:
    """Bounded 0..1 cost-efficiency score: ``REF_COST / (REF_COST + chunk_cost)``
    where ``chunk_cost = chunk_seconds/3600 * price_per_hour``.  Cheaper-to-
    render -> higher, but bounded: a chunk costing REF_COST ($0.10) scores
    0.5, dirt-cheap approaches 1.0, pricey approaches 0.  Bounding keeps cost
    on the same 0..1 scale as cuda/os -- it nudges the ranking without ever
    burying the (unbounded) speed factor, so a dirt-cheap-but-weak card can
    no longer win on price alone.

    ``price <= 0`` (unknown / free, e.g. community) -> 1.0: a free target is
    the cheapest possible, and "None means trust" matches the cuda/os factors.
    """
    if not price_per_hour or price_per_hour <= 0:
        return 1.0
    chunk_cost = (max(0.0, chunk_seconds) / 3600.0) * float(price_per_hour)
    return REF_COST / (REF_COST + chunk_cost)


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def score_target(
    *,
    render_speed: float,
    heaviness: dict[str, Any],
    estimated_chunk_frames: int,
    weights: AllocationWeights,
    cuda_version: str | None = None,
    host_os: str | None = None,
    engine: str | None = None,
    fleet: str | None = None,
    fleet_buffer_sec: float = 0.0,
    available_seconds: float | None = None,
    price_per_hour: float = 0.0,
) -> float:
    """Composite speed+cuda+os(+cost) score for a single target.  Higher = better.

    ``fleet`` and ``fleet_buffer_sec`` together model fleet-specific
    startup latency (Vast provisioning ~180s, Modal cold start ~120s,
    community 0s).  Caller passes the right buffer for the target's
    fleet.

    ``available_seconds`` is the target's remaining commitment window
    (None = unbounded).  Phase 5 multiplies the base composite by a
    headroom factor in [0, 1]: targets with comfortable headroom get
    full credit, tighter fits scale linearly toward 0.  None
    short-circuits to 1.0 (no penalty for legacy / unknown).
    """
    seconds = chunk_seconds_for(
        render_speed=render_speed,
        heaviness=heaviness,
        estimated_chunk_frames=estimated_chunk_frames,
        fleet_buffer_sec=fleet_buffer_sec,
    )
    speed_factor = REF_SECONDS / max(seconds, _MIN_CHUNK_SECONDS)
    base = (
        weights.speed_weight * speed_factor
        + weights.cuda_weight * cuda_factor(cuda_version)
        + weights.os_weight * os_factor(host_os, engine)
        + weights.cost_weight * cost_factor(seconds, price_per_hour)
    )
    return base * time_headroom_factor(
        available_seconds=available_seconds,
        chunk_seconds=seconds,
        falloff=weights.time_headroom_falloff,
    )


def time_headroom_factor(
    *,
    available_seconds: float | None,
    chunk_seconds: float,
    falloff: float,
) -> float:
    """Phase 5 headroom factor in [0, 1].

    None available_seconds -> 1.0 (legacy / unknown; no penalty).
    Otherwise: ratio = available / chunk; factor = min(1, ratio/(1+falloff)).
    With falloff=0.5, a window of ~1.5x chunk_seconds saturates the
    factor at 1.0; tighter fits scale linearly toward 0.
    """
    if available_seconds is None:
        return 1.0
    if chunk_seconds <= 0:
        return 1.0
    ratio = max(0.0, float(available_seconds)) / chunk_seconds
    denom = 1.0 + max(0.0, float(falloff))
    return min(1.0, ratio / denom)


def chunk_seconds_for(
    *,
    render_speed: float,
    heaviness: dict[str, Any],
    estimated_chunk_frames: int,
    fleet_buffer_sec: float = 0.0,
) -> float:
    """Estimated wall-time (seconds) for a chunk of N frames on a target.

    Splits cleanly into per-chunk startup (paid once) + per-frame work.
    The fleet-specific startup buffer (Vast provisioning, Modal cold
    start) is added ON TOP of the heaviness-based startup estimate.
    """
    spf = estimate_seconds_per_frame(heaviness, render_speed)
    startup = estimate_startup_seconds(heaviness) + max(0.0, float(fleet_buffer_sec))
    return startup + spf * max(0, int(estimated_chunk_frames))


def chunk_cost_for(
    *,
    render_speed: float,
    price_per_hour: float,
    heaviness: dict[str, Any],
    estimated_chunk_frames: int,
    fleet_buffer_sec: float = 0.0,
) -> float:
    """Estimated USD cost for a chunk on a target.  Telemetry only --
    not a scoring factor.  Hourly billing is Vast/Modal's model;
    community machines are nominally free at the moment (price_per_hour
    very low) but the formula still applies.
    """
    seconds = chunk_seconds_for(
        render_speed=render_speed,
        heaviness=heaviness,
        estimated_chunk_frames=estimated_chunk_frames,
        fleet_buffer_sec=fleet_buffer_sec,
    )
    return (seconds / 3600.0) * max(0.0, float(price_per_hour or 0.0))
