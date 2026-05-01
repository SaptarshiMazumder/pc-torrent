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

    # ---- create ----

    def create(self, params: CreateJobParams) -> str:
        execute(
            """
            INSERT INTO jobs (
                id, machine_id, machine_type, gpu_type,
                group_id, input_filename, status,
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, submitted_at,
                price_per_hour_at_dispatch
            )
            VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,0,'[]',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                params.job_id, params.machine_id, params.fleet, params.gpu_type,
                params.group_id, params.input_filename, params.total_frames,
                params.frame_start, params.frame_end, params.frame_step,
                params.render_overrides_json, params.attempt, params.max_retries,
                params.priority, params.chunk_index, _now_iso(),
                params.price_per_hour_at_dispatch,
            ),
        )
        return params.job_id

    # ---- reads ----

    def get_by_id(self, job_id: str) -> RenderJob | None:
        row = self._raw_with_machine_type(job_id)
        return RenderJob.from_row(row) if row else None

    def get_raw_by_id(self, job_id: str) -> dict[str, Any] | None:
        return self._raw_with_machine_type(job_id)

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

    # ---- status mutations ----

    def update_status(self, job_id: str, status: str, *, error: str | None = None) -> None:
        if status in ("done", "failed", "cancelled"):
            execute(
                "UPDATE jobs SET status = %s, completed_at = %s, error = %s WHERE id = %s",
                (status, _now_iso(), error, job_id),
            )
        else:
            execute("UPDATE jobs SET status = %s WHERE id = %s", (status, job_id))

    def mark_done(self, job_id: str) -> None:
        execute(
            "UPDATE jobs SET status = 'done', completed_at = %s WHERE id = %s",
            (_now_iso(), job_id),
        )

    def mark_failed(self, job_id: str, error: str) -> None:
        execute(
            "UPDATE jobs SET status = 'failed', completed_at = %s, error = %s WHERE id = %s",
            (_now_iso(), error, job_id),
        )

    def cancel_active_by_group(self, group_id: str) -> None:
        execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = %s "
            "WHERE group_id = %s AND status IN ('pending', 'running')",
            (_now_iso(), group_id),
        )

    # ---- retry / failover ----

    def set_retry(
        self, job_id: str, next_attempt: int, max_retries: int,
        error: str, new_frame_start: int,
    ) -> None:
        execute(
            """
            UPDATE jobs
            SET status = 'pending', attempt = %s, rendered_frames = 0,
                error = %s, frame_start = %s
            WHERE id = %s
            """,
            (next_attempt, f"Retry {next_attempt}/{max_retries} (was: {error})", new_frame_start, job_id),
        )

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
                total_frames, rendered_frames, output_files,
                frame_start, frame_end, frame_step,
                render_overrides_json, attempt, max_retries, priority,
                chunk_index, submitted_at
            )
            VALUES (%s,%s,'windows',%s,%s,'pending',%s,0,'[]',%s,%s,%s,%s,0,%s,%s,%s,%s)
            """,
            (
                new_job_id, failover_machine_id, group_id, input_filename,
                total_frames, frame_start, frame_end, frame_step,
                render_overrides_json, max_retries, priority,
                chunk_index, _now_iso(),
            ),
        )

    # ---- output files ----

    def get_output_files(self, job_id: str) -> list[str]:
        row = query_one("SELECT output_files FROM jobs WHERE id = %s", (job_id,))
        if not row:
            return []
        import json
        try:
            parsed = json.loads(row.get("output_files") or "[]")
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def merge_output_files(self, job_id: str, new_files: list[str]) -> list[str]:
        import json
        existing = self.get_output_files(job_id)
        merged = list(dict.fromkeys(existing + new_files))
        execute(
            "UPDATE jobs SET output_files = %s WHERE id = %s",
            (json.dumps(merged), job_id),
        )
        return merged

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

    def claim_next_for_machine(self, machine_id: str) -> dict[str, Any] | None:
        from serverV2.infrastructure.db import execute_returning
        # Liveness signal lives in Redis (MachineHeartbeatRepository).  The
        # /machines/{id}/heartbeat route writes there; no need to update
        # machines.last_seen_at on every poll.
        job = execute_returning(
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
        if job:
            # Lock the machine so the allocator stops returning it as
            # available for the next dispatch.  Released in lifecycle on
            # success/failure/cancel; auto-demoted to 'idle' by the
            # stale-sweep in MachineRepository if the agent crashes.
            execute(
                "UPDATE machines SET status = 'processing' WHERE id = %s",
                (machine_id,),
            )
        return job

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
                total_frames, rendered_frames, output_files,
                submitted_at, user_id
            )
            VALUES (%s,%s,%s,'uploading',0,0,'[]',%s,%s)
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
        allowed = {"runpod_job_id", "modal_function_call_id"}
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
