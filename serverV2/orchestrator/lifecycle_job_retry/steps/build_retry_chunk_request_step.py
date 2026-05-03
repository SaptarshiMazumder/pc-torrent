"""BuildRetryChunkRequestStep — shared between auto and manual pipelines.

Constructs the ``ChunkRequest`` value object from ctx fields previously
populated by load/resolve/compute steps.  ``excluded_*`` fields come
straight from ``ctx.exclusions`` (pre-resolved by the caller before the
pipeline ran — anti-affinity is never a pipeline concern).

# SOURCE: retry_dispatcher.py:117-129 (legacy auto)
# SOURCE: lifecycle.py:401-413 (legacy manual)
"""

from __future__ import annotations

from serverV2.allocation.allocation_strategies.allocation_helpers.allocation_chunk_request import (
    AllocationChunkRequest,
)
from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class BuildRetryChunkRequestStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        if ctx.remaining is None:
            return
        frame_start, frame_end, frame_step = ctx.remaining
        total_frames = ((frame_end - frame_start) // frame_step) + 1
        ctx.chunk_request = AllocationChunkRequest(
            group_id=ctx.group_id,
            chunk_index=ctx.chunk_index,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            attempt=ctx.next_attempt,
            excluded_machine_ids=ctx.exclusions.excluded_machine_ids,
            excluded_serverless_capabilities=(
                ctx.exclusions.excluded_serverless_capabilities
            ),
            file_size_bytes=ctx.file_size_bytes,
            engine=ctx.engine,
        )
