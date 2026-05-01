"""OutputFrameRepository — single source of truth for rendered frames.

Each row is one (group_id, filename) tuple plus the job_id that uploaded
it.  PRIMARY KEY (group_id, filename) makes the table self-deduplicating:
when sibling retries for the same chunk both upload ``frame0045.png``,
the second insert hits the conflict and silently no-ops.

Replaces the legacy ``jobs.output_files`` JSON-array column and the
Python set-union dedup in ``LifecycleFramesDeduplication``.  All
counters and listers (per-job, per-group, per-chunk) read straight
SQL — no parsing, no in-memory union.
"""

from __future__ import annotations

from serverV2.infrastructure.db import execute, query_all


class OutputFrameRepository:

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def add_many(
        self, group_id: str, job_id: str, filenames: list[str],
    ) -> list[str]:
        """Insert one row per filename.  Returns the filenames that were
        actually inserted (the ON CONFLICT path returns nothing) — empty
        list means a sibling already uploaded every frame in this batch.
        """
        if not filenames:
            return []
        # Build one big INSERT … VALUES (...), (...), … with shared
        # group_id/job_id and per-row filename.  Single round-trip.
        placeholders = ",".join(["(%s, %s, %s, now())"] * len(filenames))
        params: list[str] = []
        for f in filenames:
            params.extend([group_id, f, job_id])
        rows = query_all(
            f"""
            INSERT INTO output_frames (group_id, filename, job_id, created_at)
            VALUES {placeholders}
            ON CONFLICT (group_id, filename) DO NOTHING
            RETURNING filename
            """,
            tuple(params),
        )
        return [r["filename"] for r in rows]

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def count_for_group(self, group_id: str) -> int:
        rows = query_all(
            "SELECT COUNT(*) AS n FROM output_frames WHERE group_id = %s",
            (group_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    def count_for_job(self, job_id: str) -> int:
        rows = query_all(
            "SELECT COUNT(*) AS n FROM output_frames WHERE job_id = %s",
            (job_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    def list_for_group(self, group_id: str) -> list[str]:
        rows = query_all(
            "SELECT filename FROM output_frames WHERE group_id = %s "
            "ORDER BY filename",
            (group_id,),
        )
        return [r["filename"] for r in rows]

    def list_for_job(self, job_id: str) -> list[str]:
        rows = query_all(
            "SELECT filename FROM output_frames WHERE job_id = %s "
            "ORDER BY filename",
            (job_id,),
        )
        return [r["filename"] for r in rows]

    def unique_filenames_for_chunk(
        self, group_id: str, chunk_index: int,
    ) -> set[str]:
        """Distinct filenames uploaded by ANY job that worked on this
        chunk_index in this group.  Joins through ``jobs`` so callers
        don't have to enumerate sibling job_ids themselves.

        Used by the retry path to compute "what frames are still
        missing for this chunk" — set-difference between expected
        frames and this set.
        """
        rows = query_all(
            """
            SELECT DISTINCT of.filename
              FROM output_frames of
              JOIN jobs j ON j.id = of.job_id
             WHERE of.group_id    = %s
               AND j.chunk_index  = %s
            """,
            (group_id, chunk_index),
        )
        return {r["filename"] for r in rows}

    def latest_for_group(self, group_id: str) -> tuple[str, str] | None:
        """Most recently uploaded (filename, job_id) for the group, by
        ``created_at``.  Used to populate the terminal-snapshot's
        ``latest_output_file`` / ``latest_output_job_id`` fields.
        Returns None if the group has no frames yet.
        """
        rows = query_all(
            """
            SELECT filename, job_id
              FROM output_frames
             WHERE group_id = %s
             ORDER BY created_at DESC, filename DESC
             LIMIT 1
            """,
            (group_id,),
        )
        if not rows:
            return None
        return rows[0]["filename"], rows[0]["job_id"]
