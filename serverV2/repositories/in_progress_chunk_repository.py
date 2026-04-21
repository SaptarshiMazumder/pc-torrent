"""InProgressChunkRepository — ledger of chunks currently being worked on.

One row per (group_id, chunk_index).  Updated as jobs are dispatched/retried,
deleted on success or exhausted retries.

Used by the orchestrator to deduplicate retries: a failure signal arriving for
a job that is no longer the ``current_job_id`` for its chunk is stale and
MUST NOT spawn another retry.
"""

from __future__ import annotations

from typing import Any

from serverV2.infrastructure.db import execute, query_one


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

    def release_all(self, group_id: str) -> None:
        execute(
            "DELETE FROM in_progress_chunks WHERE group_id = %s",
            (group_id,),
        )
