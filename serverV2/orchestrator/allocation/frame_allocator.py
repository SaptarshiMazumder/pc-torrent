"""FrameAllocator — the single source of allocation truth.

Both initial dispatch (``allocate_initial``) and retries
(``allocate_retry``) go through this interface.  Implementations own every
decision about which fleet/machine a chunk should run on.  Future rules
(user tiers, GPU constraints, verifiability, price caps, anti-affinity
on retries, heaviness-aware sizing) live inside implementations — callers
never select machines themselves.

Inputs are :class:`AvailableResources`, a unified view of:
  * ``community_machines`` — concrete PCs with stable identity
  * ``serverless_capabilities`` — provisioning blueprints for Modal/Vast
  * ``serverless_in_flight`` — live count of pending+running jobs per fleet

Outputs are :class:`PlannedTask`s — fleet-discriminated dispatch units.
"""

from __future__ import annotations

from typing import Protocol

from serverV2.core.models import AvailableResources, PlannedTask
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest


class FrameAllocator(Protocol):

    def allocate_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        resources: AvailableResources,
        file_size_bytes: int | None = None,
        engine: str | None = None,
        tier_budget_usd: float | None = None,
        heaviness: dict | None = None,
    ) -> list[PlannedTask]:
        """Split the frame range into chunks and assign each a fleet target.

        ``file_size_bytes`` is the heaviness signal — strategies that care
        about scene weight (e.g. FastRender) read it; strategies that don't
        (Default) ignore it.  ``None`` means heaviness is unknown.

        ``engine`` is forwarded to the strategy's TargetValidators so they
        can drop fleets that cannot run the engine (e.g. Modal for EEVEE).

        ``tier_budget_usd`` is an optional soft cost cap.  Strategies that
        care (FastRender's Standard tier) drop expensive targets when the
        cost analyzer says the mix exceeds the budget by a margin.
        ``None`` disables the soft cap (default — current behaviour).

        ``heaviness`` is the parsed analysis_snapshot["heaviness"] dict
        (Phase 2).  Required by the soft budget cap; pass-through ignored
        by strategies that don't enforce one.
        """
        ...

    def allocate_retry(
        self,
        chunk_request: ChunkRequest,
        resources: AvailableResources,
    ) -> PlannedTask | None:
        """Pick a target for a single chunk that needs to be re-dispatched.

        Heaviness for retry comes from ``chunk_request.file_size_bytes``.
        Returns ``None`` when no eligible target exists.
        """
        ...
