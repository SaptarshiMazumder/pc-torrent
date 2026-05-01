"""RetryDispatcher — the auto-retry decision logic, extracted from
RenderLifecycle._try_dispatch_retry.

Public surface:

* ``attempt(ctx) -> bool`` -- decides whether to retry, dispatches a
  new job if so.  Called by ``TryRetryStep``.  Returns True iff a
  retry was dispatched (caller writes ``ctx.retried`` from the result).

The retry-specific helpers (compute remaining frames, anti-affinity
exclusions, dispatch context loading) live here too -- they're shared
with ``RenderLifecycle.retry_chunk_manually`` which calls them via
this class instead of duplicating the SQL.

Strategy selection (`_pick_strategy`) stays on RenderLifecycle because
it's also used by ``start_render`` (initial allocation, not retry).
This dispatcher accepts a callable ``strategy_picker`` so it doesn't
import the three strategy types directly.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from serverV2.core.models import (
    AvailableResources,
    DispatchContext,
    RenderJob,
)
from serverV2.orchestrator.allocation import tiers
from serverV2.orchestrator.allocation.chunk_request import ChunkRequest
from serverV2.orchestrator.allocation.frame_allocator import FrameAllocator
from serverV2.orchestrator.config import MAX_RETRIES
from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository

log = logging.getLogger(__name__)

_FRAME_FILENAME_RE = re.compile(r"frame(\d+)\.")


class RetryDispatcher:

    def __init__(
        self,
        *,
        job_repo: JobRepository,
        group_repo: RenderGroupRepository,
        output_frame_repo: OutputFrameRepository,
        coordinator: DispatchCoordinator,
        strategy_picker: Callable[[str, int, int], FrameAllocator],
        resource_picker: Callable[[], AvailableResources],
    ) -> None:
        self._job_repo = job_repo
        self._group_repo = group_repo
        self._output_frames = output_frame_repo
        self._coordinator = coordinator
        self._strategy_picker = strategy_picker
        self._resource_picker = resource_picker

    # ------------------------------------------------------------------
    # public: the retry decision (called by TryRetryStep)
    # ------------------------------------------------------------------

    def attempt(
        self, *, job_id: str, raw: dict[str, Any], error: str,
    ) -> bool:
        """Decide whether to retry this failed/cancelled job.  Returns
        True iff a new retry job was enqueued+dispatched.

        This is the body of the old RenderLifecycle._try_dispatch_retry
        with no behavior change; just relocated and given a stable
        public surface for callers.
        """
        rj = RenderJob.from_row(raw)
        group_id = rj.group_id
        chunk_index = rj.chunk_index or 0

        grp = self._group_repo.get_by_id(group_id)
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info(
                "Group %s is %s — not requeuing job %s",
                group_id, grp["status"], job_id,
            )
            return False

        # Compute remaining frames from the union of ALL sibling
        # attempts' outputs (output_frames table is self-deduplicating
        # via PK on (group_id, filename) — sibling retries that
        # uploaded the same frame collapse to one row).
        dedup_remaining = self.compute_remaining_for_chunk(group_id, chunk_index)
        if dedup_remaining is None:
            return False

        next_attempt = (rj.attempt or 0) + 1
        if next_attempt > MAX_RETRIES:
            log.warning(
                "Job %s: max retries (%d) exhausted for frames %d-%d",
                job_id, MAX_RETRIES, dedup_remaining[0], dedup_remaining[1],
            )
            return False

        frame_start, frame_end, frame_step = dedup_remaining
        total_frames = ((frame_end - frame_start) // frame_step) + 1

        # Anti-affinity: don't retry on the same fleet/gpu_type or
        # community machine that just failed.
        excluded_caps, excluded_ids = self.exclusions_for(raw)

        file_size_bytes, engine, tier = self.load_group_dispatch_context(grp)

        chunk_request = ChunkRequest(
            group_id=group_id,
            chunk_index=chunk_index,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            total_frames=total_frames,
            attempt=next_attempt,
            excluded_machine_ids=excluded_ids,
            excluded_serverless_capabilities=excluded_caps,
            file_size_bytes=file_size_bytes,
            engine=engine,
        )
        retry_strategy = self._strategy_picker(
            tiers.normalize(tier), file_size_bytes, total_frames,
        )
        retry_task = retry_strategy.allocate_retry(
            chunk_request, self._resource_picker(),
        )
        if retry_task is None:
            log.error(
                "Job %s: no eligible target for retry of frames %d-%d (group %s)",
                job_id, frame_start, frame_end, group_id,
            )
            return False

        target_label = (
            f"{retry_task.fleet}/{retry_task.gpu_type}"
            if retry_task.gpu_type
            else f"{retry_task.fleet}/{retry_task.machine_id}"
        )
        log.info(
            "Job %s: requeued frames %d-%d (attempt %d/%d) on %s",
            job_id, frame_start, frame_end, next_attempt, MAX_RETRIES, target_label,
        )

        if not rj.render_overrides_json:
            raise RuntimeError(
                f"job {rj.id} has no render_overrides_json — cannot retry"
            )
        dispatch_context = DispatchContext(
            group_id=group_id,
            input_filename=rj.input_filename,
            render_overrides_json=rj.render_overrides_json,
            blend_url="",
            max_retries=rj.max_retries,
            priority=rj.priority,
            engine=engine,
        )
        self._coordinator.enqueue_and_flush(group_id, [retry_task], dispatch_context)
        return True

    # ------------------------------------------------------------------
    # public helpers — also used by RenderLifecycle.retry_chunk_manually
    # ------------------------------------------------------------------

    def compute_remaining_for_chunk(
        self, group_id: str, chunk_index: int,
    ) -> tuple[int, int, int] | None:
        """Returns ``(frame_start, frame_end, frame_step)`` of the actually-
        missing frame range for this chunk, or None if every frame in the
        original range has already been rendered (by any sibling).

        The canonical chunk range is taken from the lowest-attempt sibling
        job's row.  Already-rendered filenames come straight from the
        ``output_frames`` table (PK on (group_id, filename) makes the
        contents self-deduplicating across siblings — we never have to
        union JSON arrays in Python).

        The returned range is contiguous from the earliest missing frame
        to the chunk's end.  Mid-chunk gaps (rare in practice — workers
        render sequentially) get included via this contiguous shape rather
        than scattered into N parallel single-frame retries.
        """
        siblings = [
            j for j in self._job_repo.get_raw_by_group(group_id)
            if (j.get("chunk_index") or 0) == chunk_index
        ]
        if not siblings:
            return None

        original = min(siblings, key=lambda j: j.get("attempt") or 0)
        chunk_start = int(original.get("frame_start") or 0)
        chunk_end = int(original.get("frame_end") or 0)
        step = int(original.get("frame_step") or 1)

        rendered_filenames = self._output_frames.unique_filenames_for_chunk(
            group_id, chunk_index,
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

    @staticmethod
    def exclusions_for(
        job_row: dict[str, Any],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        """Return ``(excluded_serverless_capabilities, excluded_machine_ids)``
        for anti-affinity on retry.  Pure function — moved out of the
        lifecycle as part of the retry-helpers consolidation."""
        fleet = (job_row.get("machine_type") or "").strip()
        gpu_type = (job_row.get("gpu_type") or "").strip()
        machine_id = (job_row.get("machine_id") or "").strip()
        if fleet in ("modal_serverless", "vast_serverless") and gpu_type:
            return ((fleet, gpu_type),), ()
        if machine_id:
            return (), (machine_id,)
        return (), ()

    @staticmethod
    def load_group_dispatch_context(
        grp: dict[str, Any] | None,
    ) -> tuple[int | None, str | None, str | None]:
        """Pull (file_size_bytes, engine, tier) off a render_groups row
        for the dispatch path.  Used by both auto-retry and
        manual-retry."""
        file_size_bytes: int | None = None
        engine: str | None = None
        tier: str | None = None
        if grp is None:
            return file_size_bytes, engine, tier

        raw_size = grp.get("r2_input_size_bytes")
        if raw_size is not None:
            try:
                file_size_bytes = int(raw_size)
            except (TypeError, ValueError):
                file_size_bytes = None

        raw_overrides = grp.get("render_overrides_json")
        if raw_overrides:
            parsed = json.loads(raw_overrides)
            if isinstance(parsed, dict):
                render_section = parsed.get("render")
                if isinstance(render_section, dict):
                    engine_value = render_section.get("engine")
                    if isinstance(engine_value, str):
                        engine = engine_value

        tier_raw = grp.get("tier")
        if isinstance(tier_raw, str):
            tier = tier_raw

        return file_size_bytes, engine, tier
