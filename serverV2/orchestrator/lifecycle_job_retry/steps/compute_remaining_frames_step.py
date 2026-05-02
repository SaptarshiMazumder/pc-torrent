"""ComputeRemainingFramesStep — shared between auto and manual pipelines.

Delegates to ``ChunkProgressService`` to compute the un-rendered frame
range for this chunk.  Stores ``(frame_start, frame_end, frame_step)``
on ``ctx.remaining``, or ``None`` if the chunk is fully rendered (or
has no siblings yet).

The decision of what to do when ``remaining is None`` is delegated to
downstream steps:
* Auto: ``AbortIfNoRemainingStep`` sets ``ctx.aborted = True``.
* Manual: ``ManualRetryRaiseIfNoRemainingStep`` raises
  ``ManualRetryError("no_remaining_frames")``.

Anti-affinity-style separation of concerns: the chunk-progress service
owns the canonical-range + uploaded-set + missing-frames computation;
this step just calls it and forwards the answer to ctx.
"""

from __future__ import annotations

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


class ComputeRemainingFramesStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        ctx.remaining = self._compute(ctx)

    @staticmethod
    def _compute(ctx: RetryContext) -> tuple[int, int, int] | None:
        siblings = [
            j for j in ctx.deps.job_repo.get_raw_by_group(ctx.group_id)
            if (j.get("chunk_index") or 0) == ctx.chunk_index
        ]
        if not siblings:
            return None
        progress = ctx.deps.chunk_progress.progress_for_chunk(
            ctx.group_id, ctx.chunk_index, siblings,
        )
        return progress.remaining_range
