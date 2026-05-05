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

Cost is NOT a scoring axis.  ``chunk_cost_for()`` stays for telemetry
(per-chunk USD estimate stamped on every PlannedTask) but is not folded
into the rank.  Cost re-enters at dispatch time as queue priority.

Composite shape::

    chunk_seconds = startup(heaviness, fleet) + spf(heaviness, render_speed) * frames
    speed_factor  = REF_SECONDS / max(chunk_seconds, 1.0)
    cuda_factor   = 0..1 from cuda_max_good (None = trust = 1.0)
    os_factor     = 0..1 from host_os + engine (None = trust = 1.0)
    score         = w.speed * speed_factor + w.cuda * cuda_factor + w.os * os_factor

Speed dominates because its factor scales with chunk wall time (often
many multiples of 1.0 for fast chunks).  CUDA and OS factors live in
0..1 and act as tiebreakers among similar-speed targets -- e.g. two RTX
4090 offers with identical render_speed but different driver vintage.

The reference value normalises the speed factor so a baseline scene on
baseline hardware yields ~1.0.
"""

from __future__ import annotations

from typing import Any

from serverV2.allocation.allocation_strategies.allocation_weights import (
    AllocationWeights,
)
from serverV2.allocation.allocation_strategies.analyzers.allocation_time_analyzer import (
    estimate_seconds_per_frame,
    estimate_startup_seconds,
)


# 5 minutes of render = 300s.  A target completing a chunk in 300s
# yields speed_factor = 1.0; faster -> >1.0.
REF_SECONDS = 300.0

# Floors to avoid divide-by-zero / runaway scores on misconfigured inputs.
_MIN_CHUNK_SECONDS = 1.0
_MIN_CHUNK_COST = 0.001

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
) -> float:
    """Composite speed+cuda+os score for a single target.  Higher = better.

    ``fleet`` and ``fleet_buffer_sec`` together model fleet-specific
    startup latency (Vast provisioning ~180s, Modal cold start ~120s,
    community 0s).  Caller passes the right buffer for the target's
    fleet.
    """
    seconds = chunk_seconds_for(
        render_speed=render_speed,
        heaviness=heaviness,
        estimated_chunk_frames=estimated_chunk_frames,
        fleet_buffer_sec=fleet_buffer_sec,
    )
    speed_factor = REF_SECONDS / max(seconds, _MIN_CHUNK_SECONDS)
    return (
        weights.speed_weight * speed_factor
        + weights.cuda_weight * cuda_factor(cuda_version)
        + weights.os_weight * os_factor(host_os, engine)
    )


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
