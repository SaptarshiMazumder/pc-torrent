"""AllocationEngineCompatibilityValidator — exclude fleets that can't run the engine.

Some fleet runtimes don't ship NVIDIA's user-space graphics drivers
(no ``libEGL_nvidia.so.0``, no ``libGLX_nvidia.so.0``, no Linux Vulkan
ICD).  EEVEE needs a GPU graphics context (OpenGL/EGL or Vulkan) and
hard-fails on those fleets.  Cycles uses CUDA directly and is unaffected.

Currently:
  * ``modal_serverless`` — Modal exposes only compute caps, no graphics.
  * ``community``        — Windows + Docker Desktop + WSL2.  We proved
                            empirically that NVIDIA Container Toolkit on
                            this stack mounts only compute libraries:
                            no EGL, no GLX, no Vulkan ICD make it into
                            the container regardless of
                            ``NVIDIA_DRIVER_CAPABILITIES``.  Native
                            Windows Blender (out-of-Docker) is the
                            future fix; until then community can't run
                            EEVEE.
  * ``vast_serverless``  — host-dependent; most hosts work, retry
                            handles the few that don't, so we DO NOT
                            block it here.
"""

from __future__ import annotations

from serverV2.core.models import CommunityMachine, FleetCapability
from serverV2.allocation.allocation_strategies.validators.allocation_validation_context import (
    AllocationValidationContext,
)


_EEVEE_ENGINES = frozenset({"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"})
_FLEETS_WITHOUT_GRAPHICS_CAPS = frozenset({"modal_serverless", "community"})


class AllocationEngineCompatibilityValidator:

    def is_valid(self, target, context: AllocationValidationContext) -> bool:
        engine = context.engine
        if not engine:
            # Required upstream invariant: engine MUST be set before
            # allocation runs.  Falling back silently masks a real bug
            # (e.g. Modal getting EEVEE jobs because the validator can't
            # decide).  Crash loud so the missing plumbing is visible.
            raise ValueError(
                "AllocationEngineCompatibilityValidator requires context.engine to be set. "
                "Upstream did not plumb the render engine through "
                "(check lifecycle.plan -> ChunkRequest.engine -> ValidationContext.engine)."
            )
        if engine.upper() not in _EEVEE_ENGINES:
            return True
        if isinstance(target, FleetCapability):
            fleet = target.fleet
        elif isinstance(target, CommunityMachine):
            fleet = "community"
        else:
            raise TypeError(
                f"AllocationEngineCompatibilityValidator got unsupported target type: {type(target).__name__}"
            )
        return fleet not in _FLEETS_WITHOUT_GRAPHICS_CAPS
