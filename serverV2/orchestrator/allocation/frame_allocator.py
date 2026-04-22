"""FrameAllocator — the single source of machine-allocation truth.

Both initial dispatch (``allocate_initial``) and retries
(``allocate_retry``) go through this interface.  Implementations own every
decision about which machine a chunk should run on.  Future rules (user
tiers, GPU constraints, verifiability, price caps, anti-affinity on retries)
live inside implementations of this interface — callers never select
machines themselves.
"""

from __future__ import annotations

from typing import Protocol

from serverV2.core.models import Machine, PlannedTask
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest


class FrameAllocator(Protocol):

    def allocate_initial(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        machines: list[Machine],
    ) -> list[PlannedTask]:
        """Split the frame range into chunks and assign a machine to each."""
        ...

    def allocate_retry(
        self,
        chunk_request: ChunkRequest,
        machines: list[Machine],
    ) -> PlannedTask | None:
        """Pick a machine for a single chunk that needs to be re-dispatched.

        Returns None when no machine in the pool is eligible.
        """
        ...
