"""SuccessHandler — marks a job done, drains the queue, asks the
orchestrator to roll the change up to the group.

Job-level mutations (mark_done, in-progress release, fleet drain) live
here.  Group-level state changes go through the orchestrator — this
handler never touches ``render_groups`` directly.

After mark_done, writes a row to ``render_telemetry`` (Phase 5) when
the job has the price_per_hour_at_dispatch + started_at stamps it
needs.  Telemetry is skipped silently for legacy jobs lacking those
columns and for community jobs (which today don't carry a price stamp).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.render_group_repository import RenderGroupRepository
from serverV2.repositories.telemetry_repository import TelemetryRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.dispatch.coordinator import DispatchCoordinator
    from serverV2.orchestrator.orchestrator import RenderOrchestrator

log = logging.getLogger(__name__)


class SuccessHandler:

    def __init__(
        self,
        job_repo: JobRepository,
        in_progress_repo: InProgressChunkRepository,
        group_repo: RenderGroupRepository,
        telemetry_repo: TelemetryRepository,
    ) -> None:
        self._job_repo = job_repo
        self._in_progress = in_progress_repo
        self._group_repo = group_repo
        self._telemetry = telemetry_repo
        self._orchestrator: RenderOrchestrator | None = None
        self._coordinator: DispatchCoordinator | None = None

    def set_orchestrator(self, orchestrator: "RenderOrchestrator") -> None:
        """Late-bound to break the circular wiring with the orchestrator
        (lifecycle composes the orchestrator + handlers, so the handlers
        cannot take it via __init__)."""
        self._orchestrator = orchestrator

    def set_coordinator(self, coordinator: "DispatchCoordinator") -> None:
        """Late-bound to break the circular wiring with the coordinator."""
        self._coordinator = coordinator

    def handle(self, job_id: str, group_id: str) -> None:
        # Release the chunk from the in-progress ledger FIRST so any late
        # failure signal for this job is recognized as stale and ignored.
        raw = self._job_repo.get_raw_by_id(job_id)
        fleet = (raw.get("machine_type") or "") if raw else ""
        if raw is not None:
            chunk_index = raw.get("chunk_index") or 0
            self._in_progress.release(group_id, chunk_index)

        self._job_repo.mark_done(job_id)
        log.info("Job %s marked done", job_id)

        # Phase 5 telemetry — record one row per successful chunk.  Reads
        # the freshly-mark-done job row so completed_at is current.
        if raw is not None:
            try:
                self._record_telemetry(job_id, group_id, raw)
            except Exception as exc:
                # Telemetry must never break the success path.  Log + move on.
                log.warning("Telemetry write failed for job %s: %s", job_id, exc)

        if self._orchestrator is not None and group_id:
            self._orchestrator.on_job_succeeded(group_id)

        # A slot just opened up in this fleet — drain any waiting items.
        if fleet and self._coordinator is not None:
            try:
                self._coordinator.drain_for_fleet(fleet)
            except Exception as exc:
                log.warning("drain_for_fleet(%s) failed: %s", fleet, exc)

    # ------------------------------------------------------------------
    # Telemetry helpers
    # ------------------------------------------------------------------

    def _record_telemetry(
        self,
        job_id: str,
        group_id: str,
        raw_pre_done: dict[str, Any],
    ) -> None:
        """Insert one render_telemetry row for this just-completed chunk.

        Skipped (logged at info, not warning) when:
          * price_per_hour_at_dispatch is missing — community jobs in v1
            and any job dispatched before Phase 5 landed.
          * started_at is missing — chunk never reported a PROGRESS event
            (shouldn't happen for a successful chunk, but defensive).
        """
        price = raw_pre_done.get("price_per_hour_at_dispatch")
        started_at = raw_pre_done.get("started_at")
        if price is None or started_at is None:
            log.info(
                "Skipping telemetry for job %s — missing %s",
                job_id,
                "price_per_hour_at_dispatch" if price is None else "started_at",
            )
            return

        # Re-fetch to get the now-stamped completed_at written by mark_done.
        post_done = self._job_repo.get_raw_by_id(job_id) or raw_pre_done
        completed_at = post_done.get("completed_at") or _now_iso()

        # Compute seconds_total — both timestamps come from Postgres so
        # parse them defensively (tz-aware ISO strings).
        seconds_total = _seconds_between(started_at, completed_at)

        chunk_size = self._chunk_size_from_row(post_done)
        rendered_frames = int(post_done.get("rendered_frames") or 0)
        price_per_hour = float(price)
        cost_actual = (seconds_total / 3600.0) * price_per_hour

        heaviness = self._fetch_heaviness(group_id)
        file_size_bytes = self._fetch_file_size(group_id)

        self._telemetry.record_chunk(
            job_id=job_id,
            group_id=group_id,
            fleet=str(post_done.get("machine_type") or ""),
            gpu_type=post_done.get("gpu_type"),
            machine_id=post_done.get("machine_id"),
            chunk_size=chunk_size,
            rendered_frames=rendered_frames,
            started_at=_iso_str(started_at),
            completed_at=_iso_str(completed_at),
            seconds_total=seconds_total,
            price_per_hour=price_per_hour,
            cost_actual_usd=cost_actual,
            heaviness=heaviness,
            file_size_bytes=file_size_bytes,
            seconds_estimated=None,
            cost_estimated_usd=None,
        )

    def _fetch_heaviness(self, group_id: str) -> dict[str, Any]:
        try:
            group = self._group_repo.get_by_id(group_id)
            if not group:
                return {}
            raw = group.get("analysis_snapshot_json")
            if not raw:
                return {}
            parsed = json.loads(raw)
            heaviness = parsed.get("heaviness") if isinstance(parsed, dict) else None
            return heaviness if isinstance(heaviness, dict) else {}
        except Exception:
            return {}

    def _fetch_file_size(self, group_id: str) -> int | None:
        try:
            group = self._group_repo.get_by_id(group_id)
            if not group:
                return None
            value = group.get("r2_input_size_bytes")
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _chunk_size_from_row(row: dict[str, Any]) -> int:
        start = int(row.get("frame_start") or 0)
        end = int(row.get("frame_end") or 0)
        step = max(1, int(row.get("frame_step") or 1))
        if end < start:
            return 0
        return ((end - start) // step) + 1


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_str(value: Any) -> str:
    """Coerce a timestamp value (datetime or ISO string) to an ISO string."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _seconds_between(start: Any, end: Any) -> int:
    """Total seconds between two timestamps (datetime or ISO strings).
    Returns 0 on parse failure."""
    try:
        s = _to_dt(start)
        e = _to_dt(end)
        if s is None or e is None:
            return 0
        return max(0, int((e - s).total_seconds()))
    except Exception:
        return 0


def _to_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        # fromisoformat handles tz-aware strings on Python 3.11+
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
