"""InProgressChunkRepository — ledger of chunks currently being worked on.

One row per (group_id, chunk_index).  Updated as jobs are dispatched/retried,
deleted on success or exhausted retries.

Used by the orchestrator to deduplicate retries: a failure signal arriving for
a job that is no longer the ``current_job_id`` for its chunk is stale and
MUST NOT spawn another retry.
"""

from __future__ import annotations

from typing import Any

from serverV2.infrastructure.db import execute, query_all, query_one


class InProgressChunkRepository:

    def claim_or_replace(
        self,
        group_id: str,
        chunk_index: int,
        job_id: str,
        attempt: int,
    ) -> None:
        """Upsert the row for (group_id, chunk_index) to point at this job.

        Idempotent: safe to call on initial dispatch AND retry dispatch.
        """
        from serverV2.core.value_objects import now_iso
        now = now_iso()
        execute(
            """
            INSERT INTO in_progress_chunks
                (group_id, chunk_index, current_job_id, attempt, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (group_id, chunk_index) DO UPDATE
                SET current_job_id = EXCLUDED.current_job_id,
                    attempt = EXCLUDED.attempt,
                    updated_at = EXCLUDED.updated_at
            """,
            (group_id, chunk_index, job_id, attempt, now),
        )

    def current_job_for(self, group_id: str, chunk_index: int) -> str | None:
        row = query_one(
            "SELECT current_job_id FROM in_progress_chunks "
            "WHERE group_id = %s AND chunk_index = %s",
            (group_id, chunk_index),
        )
        return row["current_job_id"] if row else None

    def release(self, group_id: str, chunk_index: int) -> None:
        execute(
            "DELETE FROM in_progress_chunks WHERE group_id = %s AND chunk_index = %s",
            (group_id, chunk_index),
        )

    def release_if_owner(
        self, group_id: str, chunk_index: int, expected_job_id: str,
    ) -> bool:
        """Atomically delete the ledger row only if it still points at
        ``expected_job_id``.  Returns True if the row was deleted, False
        if it didn't exist or pointed at a different job.

        This is the dedup primitive for concurrent failure signals: when
        N callers all try to handle the same job's failure, the DB
        serializes the DELETEs and only one gets a returned row.  The
        others see 0 rows and bail without retrying -- the winner's
        retry dispatch path (which re-claims the slot via
        ``claim_or_replace``) is the only one that proceeds.
        """
        rows = query_all(
            """
            DELETE FROM in_progress_chunks
             WHERE group_id      = %s
               AND chunk_index   = %s
               AND current_job_id = %s
            RETURNING current_job_id
            """,
            (group_id, chunk_index, expected_job_id),
        )
        return bool(rows)

    def release_all(self, group_id: str) -> None:
        execute(
            "DELETE FROM in_progress_chunks WHERE group_id = %s",
            (group_id,),
        )
