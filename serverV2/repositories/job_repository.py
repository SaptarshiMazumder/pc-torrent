"""JobRepository — all SQL for the ``jobs`` table.  No business logic."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from serverV2.infrastructure.db import execute, query_all, query_one
from serverV2.core.models import CreateJobParams, RenderJob

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRepository:

    def __init__(self, terminal_cache=None) -> None:
        # Optional ``JobTerminalCache``.  When wired (production), every
        # method below that transitions a row to a terminal status also
        # marks the cache so worker heartbeats bounce with 410 without
        # doing a Postgres SELECT each time.  When None (older callers /
        # ad-hoc scripts), terminal writes still land in PG; readers
        # fall back to whatever path they had before.
        self._terminal_cache = terminal_cache

    def _mark_terminal_in_cache(self, job_id: str) -> None:
        if self._terminal_cache is not None:
            self._terminal_cache.mark_terminal(job_id)

    # ---- create ----

    def create(self, params: CreateJobParams) -> str:
        import json as _json
        allowed_stall_json = (
            _json.dumps(params.allowed_stall_times)
            if params.allowed_stall_times is not None
            else None
        )
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, machine_type, gpu_type,
                group_id, input_filename, status,
                total_frames, rendered_frames,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, submitted_at,
                price_per_hour_at_dispatch,
                estimated_seconds, estimated_cost_usd,
                estimated_seconds_per_frame, estimated_startup_seconds,
                allowed_stall_times
            )
            VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                params.job_id, params.machine_id, params.fleet, params.gpu_type,
                params.group_id, params.input_filename, params.total_frames,
                params.frame_start, params.frame_end, params.frame_step,
                params.render_overrides_json, params.attempt, params.max_retries,
                params.priority, params.chunk_index, _now_iso(),
                params.price_per_hour_at_dispatch,
                params.estimated_seconds, params.estimated_cost_usd,
                params.estimated_seconds_per_frame, params.estimated_startup_seconds,
                allowed_stall_json,
            ),
        )
        return params.job_id

    def get_allowed_stall_times(self, job_id: str) -> dict | None:
        """Return the resolved kill-time deadline dict written at
        dispatch.  None if the job doesn't exist OR pre-dates the column
        (legacy rows).  Callers treat None as "not yet known"."""
        row = query_one(
            "SELECT allowed_stall_times FROM jobs WHERE id = %s",
            (job_id,),
        )
        if row is None:
            return None
        return row.get("allowed_stall_times")

    # ---- reads ----

    def get_by_id(self, job_id: str) -> RenderJob | None:
        row = self._raw_with_machine_type(job_id)
        return RenderJob.from_row(row) if row else None

    def get_raw_by_id(self, job_id: str) -> dict[str, Any] | None:
        return self._raw_with_machine_type(job_id)

    def get_raw_by_vast_id(self, vast_id: int) -> dict[str, Any] | None:
        """Look up the local job that owns a given Vast.ai instance id.
        Used by the internal ghost-instance endpoint to defensively
        re-check status before destroying a provider-side instance."""
        row = query_one(
            "SELECT * FROM jobs WHERE vast_job_id = %s",
            (str(vast_id),),
        )
        return self._attach_machine_type(row) if row else None

    def get_raw_by_modal_function_call_id(
        self, call_id: str,
    ) -> dict[str, Any] | None:
        """Same as ``get_raw_by_vast_id`` but keyed on
        ``modal_function_call_id``."""
        row = query_one(
            "SELECT * FROM jobs WHERE modal_function_call_id = %s",
            (call_id,),
        )
        return self._attach_machine_type(row) if row else None

    def get_by_group(self, group_id: str) -> list[RenderJob]:
        rows = query_all(
            "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
            (group_id,),
        )
        return [RenderJob.from_row(self._attach_machine_type(r)) for r in rows]

    def get_raw_by_group(self, group_id: str) -> list[dict[str, Any]]:
        return query_all(
            "SELECT * FROM jobs WHERE group_id = %s ORDER BY frame_start ASC",
            (group_id,),
        )

    def get_raw_by_groups(self, group_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Batched variant of ``get_raw_by_group`` — used by the list endpoint
        to load all active groups' jobs in one query instead of N.  Returns
        a dict keyed by group_id so callers can index per-group without a
        second pass."""
        if not group_ids:
            return {}
        rows = query_all(
            "SELECT * FROM jobs WHERE group_id = ANY(%s) ORDER BY frame_start ASC",
            (list(group_ids),),
        )
        result: dict[str, list[dict[str, Any]]] = {gid: [] for gid in group_ids}
        for row in rows:
            gid = row.get("group_id")
            if gid in result:
                result[gid].append(row)
        return result

    def get_active_by_group(self, group_id: str) -> list[dict[str, Any]]:
        return query_all(
            "SELECT * FROM jobs WHERE group_id = %s AND status IN ('pending', 'running')",
            (group_id,),
        )

    def count_active_by_fleet(self) -> dict[str, int]:
        """Live capacity check.  Returns {machine_type: count} of jobs in
        ``pending`` or ``running`` state.  Used by the resource picker to
        compute how much serverless headroom each fleet has before
        ``fleet_max_parallel`` is exhausted.
        """
        rows = query_all(
            "SELECT machine_type, count(*) AS n FROM jobs "
            "WHERE status IN ('pending', 'running') "
            "GROUP BY machine_type",
        )
        return {r["machine_type"]: int(r["n"]) for r in rows if r.get("machine_type")}

    def count_active_by_fleet_and_gpu_type(self) -> dict[tuple[str, str], int]:
        """Per-(fleet, gpu_type) live count of ``pending``/``running`` jobs.
        Used as the PG fallback by ``ModalAvailabilityBuilder`` when its
        Redis-backed tracker is unreachable -- enforces the per-GPU cap
        on top of the fleet-wide ``max_parallel``.
        """
        rows = query_all(
            "SELECT machine_type, gpu_type, count(*) AS n FROM jobs "
            "WHERE status IN ('pending', 'running') "
            "GROUP BY machine_type, gpu_type",
        )
        return {
            (r["machine_type"], r["gpu_type"] or ""): int(r["n"])
            for r in rows if r.get("machine_type") and r.get("gpu_type")
        }

    # ---- status mutations ----

    def update_status(self, job_id: str, status: str, *, error: str | None = None) -> None:
        if status in ("done", "failed", "cancelled"):
            execute(
                "UPDATE jobs SET status = %s, completed_at = %s, error = %s WHERE id = %s",
                (status, _now_iso(), error, job_id),
            )
            self._mark_terminal_in_cache(job_id)
        else:
            execute("UPDATE jobs SET status = %s WHERE id = %s", (status, job_id))

    def mark_done(self, job_id: str) -> None:
        execute(
            "UPDATE jobs SET status = 'done', completed_at = %s WHERE id = %s",
            (_now_iso(), job_id),
        )
        self._mark_terminal_in_cache(job_id)

    def mark_failed(self, job_id: str, error: str) -> None:
        execute(
            "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
            (_now_iso(), error, job_id),
        )
        self._mark_terminal_in_cache(job_id)

    def cancel_active_by_group(self, group_id: str) -> None:
        rows = query_all(
            "UPDATE jobs SET status = 'cancelled', completed_at = %s "
            "WHERE group_id = %s AND status IN ('pending', 'running') "
            "RETURNING id",
            (_now_iso(), group_id),
        )
        if rows and self._terminal_cache is not None:
            self._terminal_cache.mark_terminal_many(
                [str(r["id"]) for r in rows if r.get("id")]
            )

    # ---- failover ----

    def create_failover_job(
        self,
        *,
        new_job_id: str,
        failover_machine_id: str,
        group_id: str,
        input_filename: str,
        total_frames: int,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_json: str,
        max_retries: int,
        priority: int,
        chunk_index: int | None,
    ) -> None:
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, machine_type,
                group_id, input_filename, status,
                total_frames, rendered_frames,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, submitted_at
            )
            VALUES (%s,%s,'windows',%s,%s,'pending',%s,0,%s,%s,%s,%s,0,%s,%s,%s,%s)
            """,
            (
                new_job_id, failover_machine_id, group_id, input_filename,
                total_frames, frame_start, frame_end, frame_step,
                render_overrides_json, max_retries, priority,
                chunk_index, _now_iso(),
            ),
        )

    # ---- start timestamp (for telemetry) ----

    def mark_started(self, job_id: str) -> None:
        """Stamp ``started_at = NOW()`` on the job, but only if it hasn't
        been set yet.  Idempotent — safe to call from every PROGRESS
        callback; only the first one wins.

        ``started_at`` is the moment rendering actually began (first
        PROGRESS event from the worker), not when the job was dispatched.
        Phase 5 telemetry uses ``completed_at - started_at`` as the
        per-chunk wall-time signal, excluding queue + provisioning.
        """
        execute(
            "UPDATE jobs SET started_at = %s WHERE id = %s AND started_at IS NULL",
            (_now_iso(), job_id),
        )

    # ---- next for machine (desktop agent polling) ----

    def get_running_for_machine(self, machine_id: str) -> list[dict[str, Any]]:
        """All ``status='running'`` rows assigned to this machine.  Used by
        the community-idle path to spot jobs the agent abandoned across a
        restart.  ``machine_id`` is only populated for community jobs;
        Vast/Modal rows have it null."""
        return query_all(
            "SELECT * FROM jobs WHERE machine_id = %s AND status = 'running'",
            (machine_id,),
        )

    def get_stale_pending_community(self, threshold_sec: int) -> list[dict[str, Any]]:
        """Community jobs stuck in ``status='pending'`` for longer than
        ``threshold_sec`` -- the agent never claimed the dispatch (likely
        crashed between dispatch and its next poll).  CommunityMonitor's
        equivalent of Modal's ``in_queue_timeout_sec`` and Vast's
        ``startup_timeout_sec`` detectors.

        ``submitted_at`` is stored as an ISO-8601 UTC text column (not
        timestamptz), so the comparison uses an ISO string threshold.
        UTC ISO timestamps sort lexicographically, so this is correct
        and avoids a per-row cast.
        """
        from datetime import datetime, timedelta, timezone
        threshold_iso = (
            datetime.now(timezone.utc) - timedelta(seconds=threshold_sec)
        ).isoformat()
        return query_all(
            """
            SELECT id, machine_id FROM jobs
            WHERE status = 'pending'
              AND machine_type = 'windows'
              AND submitted_at < %s
            """,
            (threshold_iso,),
        )

    def claim_next_for_machine(self, machine_id: str) -> dict[str, Any] | None:
        """Claim the oldest pending job for this machine and flip it to
        'running'.  Does NOT touch the machines row -- the caller
        (``JobService.next_for_machine``) routes the
        machines.status='processing' write through ``MachineRepository
        .update_status`` so PG (sync) and Redis mirror (async) stay in sync.
        """
        from serverV2.infrastructure.db import execute_returning
        return execute_returning(
            """
            UPDATE jobs SET status = 'running', last_heartbeat_at = %s
            WHERE id = (
                SELECT id FROM jobs
                WHERE machine_id = %s AND status = 'pending'
                ORDER BY priority DESC, submitted_at ASC
                LIMIT 1
            )
            RETURNING *
            """,
            (_now_iso(), machine_id),
        )

    # ---- user-scoped reads ----

    def get_by_user(self, user_id: str) -> list[dict[str, Any]]:
        return query_all(
            "SELECT * FROM jobs WHERE user_id = %s AND group_id IS NULL ORDER BY submitted_at DESC",
            (user_id,),
        )

    def get_owned_by_id(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        return query_one(
            "SELECT * FROM jobs WHERE id = %s AND user_id = %s",
            (job_id, user_id),
        )

    def delete_terminal(self, job_id: str) -> bool:
        row = query_one(
            "SELECT status FROM jobs WHERE id = %s",
            (job_id,),
        )
        if not row or row["status"] not in ("done", "failed", "cancelled"):
            return False
        execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        return True

    # ---- standalone job upload flow ----

    def create_uploading_job(
        self, *, job_id: str, machine_id: str, input_filename: str,
        r2_key: str, user_id: str,
    ) -> None:
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, input_filename, status,
                total_frames, rendered_frames,
                submitted_at, user_id
            )
            VALUES (%s,%s,%s,'uploading',0,0,%s,%s)
            """,
            (job_id, machine_id, input_filename, _now_iso(), user_id),
        )

    def confirm_upload(self, job_id: str) -> None:
        execute(
            "UPDATE jobs SET status = 'pending' WHERE id = %s AND status = 'uploading'",
            (job_id,),
        )

    # ---- provider tracking ----

    def save_provider_job_id(self, job_id: str, *, provider_job_id: str, column: str) -> None:
        allowed = {"vast_job_id", "modal_function_call_id"}
        if column not in allowed:
            raise ValueError(f"Invalid provider column: {column}")
        execute(f"UPDATE jobs SET {column} = %s WHERE id = %s", (provider_job_id, job_id))

    # ---- internal ----

    def _raw_with_machine_type(self, job_id: str) -> dict[str, Any] | None:
        row = query_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return self._attach_machine_type(row) if row else None

    @staticmethod
    def _attach_machine_type(row: dict[str, Any]) -> dict[str, Any]:
        if not row.get("machine_type"):
            machine = query_one(
                "SELECT machine_type FROM machines WHERE id = %s",
                (row.get("machine_id"),),
            )
            row = dict(row)
            row["machine_type"] = (machine or {}).get("machine_type", "windows")
        return row
