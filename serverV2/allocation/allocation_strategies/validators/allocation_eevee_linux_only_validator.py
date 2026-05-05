"""AllocationEeveeLinuxOnlyValidator — exclude non-Linux Vast offers
on EEVEE renders.

EEVEE needs a working OpenGL/EGL stack.  Vast Linux peers ship that
reliably (NVIDIA EGL ICD via the GLVND dispatcher).  Vast Windows peers
are an edge case where headless EGL surface init either silently
produces black-frame renders or hard-fails at kernel init.  Either
way, EEVEE on Windows Vast is wasted compute.

This validator is the hard exclusion that complements ``os_factor``'s
soft penalty in the composite scorer.  ``os_factor`` makes a Windows
EEVEE offer score lower; this validator removes it from the eligible
pool entirely so it can never be backfilled when the picker runs out
of Linux offers.

Modal and community are NOT touched here -- ``EngineCompatibilityValidator``
already excludes those fleets from EEVEE for unrelated reasons (no
graphics caps in their runtimes).  This validator only narrows the
Vast subset.
"""

from __future__ import annotations

from serverV2.allocation.allocation_strategies.validators.allocation_validation_context import (
    AllocationValidationContext,
)
from serverV2.core.models import CommunityMachine, FleetCapability


_EEVEE_ENGINES = frozenset({"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"})
_VAST_FLEET = "vast_serverless"


class AllocationEeveeLinuxOnlyValidator:

    def is_valid(self, target, context: AllocationValidationContext) -> bool:
        engine = (context.engine or "").upper()
        if engine not in _EEVEE_ENGINES:
            return True
        # Community machines and Modal capabilities are governed by
        # EngineCompatibilityValidator; we don't second-guess them
        # from the Linux-only angle.
        if not isinstance(target, FleetCapability):
            return True
        if target.fleet != _VAST_FLEET:
            return True
        # Vast offer with no host_os signal: trust it (the offer
        # search returns Linux distro versions for Vast hosts; absence
        # means analyzer didn't capture it, not "Windows").
        host_os = (target.host_os or "").lower()
        if not host_os:
            return True
        return (
            "linux" in host_os
            or "ubuntu" in host_os
            or "debian" in host_os
        )
