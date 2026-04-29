"""EngineCompatibilityValidator — exclude fleets that can't run the engine.

Some serverless fleets run in container runtimes that do NOT inject NVIDIA
graphics drivers (no ``libEGL_nvidia.so.0``, no ``libGLX_nvidia.so.0``).
EEVEE renders need an OpenGL/EGL context and fail on those fleets with
``EGL_BAD_MATCH``.  Cycles uses CUDA directly and is unaffected.

Currently:
  * ``modal_serverless`` — Modal exposes only compute caps, no graphics.
  * ``vast_serverless``  — host-dependent; most hosts work, retry handles
                            the few that don't, so we DO NOT block it here.
  * ``community``        — full desktop GPU, always works.
"""

from __future__ import annotations

from serverV2.core.models import FleetCapability
from serverV2.orchestrator.allocation.validators.validation_context import (
    ValidationContext,
)


_EEVEE_ENGINES = frozenset({"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"})
_FLEETS_WITHOUT_GRAPHICS_CAPS = frozenset({"modal_serverless"})


class EngineCompatibilityValidator:

    def is_valid(self, target, context: ValidationContext) -> bool:
        engine = context.engine
        if not engine:
            # Required upstream invariant: engine MUST be set before
            # allocation runs.  Falling back silently masks a real bug
            # (e.g. Modal getting EEVEE jobs because the validator can't
            # decide).  Crash loud so the missing plumbing is visible.
            raise ValueError(
                "EngineCompatibilityValidator requires context.engine to be set. "
                "Upstream did not plumb the render engine through "
                "(check lifecycle.plan -> ChunkRequest.engine -> ValidationContext.engine)."
            )
        if engine.upper() not in _EEVEE_ENGINES:
            return True
        if isinstance(target, FleetCapability):
            return target.fleet not in _FLEETS_WITHOUT_GRAPHICS_CAPS
        # Community machines: full GL stack, always allowed.
        return True
