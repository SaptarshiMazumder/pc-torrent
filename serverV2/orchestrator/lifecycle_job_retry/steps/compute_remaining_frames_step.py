"""ComputeRemainingFramesStep — shared between auto and manual pipelines.

Computes the un-rendered frame range for this chunk by unioning every
sibling attempt's uploaded frames against the chunk's canonical range.
Stores ``(frame_start, frame_end, frame_step)`` on ``ctx.remaining``,
or ``None`` if the chunk is fully rendered.

The decision of what to do when ``remaining is None`` is delegated to
downstream steps:
* Auto: ``AbortIfNoRemainingStep`` sets ``ctx.aborted = True``.
* Manual: ``ManualRetryRaiseIfNoRemainingStep`` raises
  ``ManualRetryError("no_remaining_frames")``.

# SOURCE: retry_dispatcher.py:173-216 (legacy compute_remaining_for_chunk)
"""

from __future__ import annotations

import re

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)


_FRAME_FILENAME_RE = re.compile(r"frame(\d+)\.")


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

        original = min(siblings, key=lambda j: j.get("attempt") or 0)
        chunk_start = int(original.get("frame_start") or 0)
        chunk_end = int(original.get("frame_end") or 0)
        step = int(original.get("frame_step") or 1)

        rendered_filenames = ctx.deps.output_frame_repo.unique_filenames_for_chunk(
            ctx.group_id, ctx.chunk_index,
        )
        rendered: set[int] = set()
        for fname in rendered_filenames:
            match = _FRAME_FILENAME_RE.match(fname)
            if match:
                rendered.add(int(match.group(1)))

        all_chunk_frames = set(range(chunk_start, chunk_end + 1, step))
        missing = sorted(all_chunk_frames - rendered)
        if not missing:
            return None
        return (missing[0], chunk_end, step)
