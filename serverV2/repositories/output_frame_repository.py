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

    def count_in_range(
        self,
        group_id: str,
        frame_start: int,
        frame_end: int,
        frame_step: int = 1,
    ) -> int:
        """Count distinct frames in ``[frame_start, frame_end]`` (stepped
        by ``frame_step``) that exist in ``output_frames`` for this group,
        regardless of which job_id uploaded them or what the filename's
        camera prefix is.

        Matches on ``frame_number`` (the DB-generated frame index parsed
        from the filename) rather than reconstructing exact filenames, so
        camera-grouped names like ``Camera-A_frame0045.png`` still count.
        ``COUNT(DISTINCT frame_number)`` is the no-double-count guarantee:
        the same frame under two different filenames (e.g. an un-prefixed
        legacy row + a prefixed retry) counts once.

        Used for chunk-level completion checks that need to see frames
        contributed by sibling retries.  ``count_for_job`` only sees rows
        under one job_id; this is group-wide and prefix-agnostic.
        """
        if frame_end < frame_start or frame_step <= 0:
            return 0
        rows = query_all(
            "SELECT COUNT(DISTINCT frame_number) AS n FROM output_frames "
            "WHERE group_id = %s AND frame_number BETWEEN %s AND %s "
            "AND (frame_number - %s) %% %s = 0",
            (group_id, frame_start, frame_end, frame_start, frame_step),
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

    def list_for_group_grouped_by_job(
        self, group_id: str,
    ) -> dict[str, list[str]]:
        """Bulk equivalent of ``{job_id: list_for_job(job_id) for ...}``
        for every job that uploaded into ``group_id``.  One query instead
        of N -- used by the render-groups detail page to avoid the per-
        job N+1 in ``serialize_task``.

        Per-job filename lists arrive sorted by filename (matches what
        ``list_for_job`` returns).  Jobs with no uploads simply don't
        appear in the result -- callers should ``dict.get(job_id, [])``.
        """
        rows = query_all(
            "SELECT job_id, filename FROM output_frames "
            "WHERE group_id = %s ORDER BY job_id, filename",
            (group_id,),
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["job_id"], []).append(r["filename"])
        return out

    def frame_numbers_for_chunk(
        self, group_id: str, chunk_index: int,
    ) -> set[int]:
        """Distinct frame numbers uploaded by ANY job that worked on this
        chunk_index in this group.  Joins through ``jobs`` so callers
        don't have to enumerate sibling job_ids themselves.

        Used by the retry path to compute "what frames are still missing
        for this chunk" — set-difference between expected frames and this
        set.  Reads the generated ``frame_number`` column, so it's
        agnostic to the filename's camera prefix.  Rows without a frame
        token (``frame_number IS NULL``) are skipped.
        """
        rows = query_all(
            """
            SELECT DISTINCT of.frame_number
              FROM output_frames of
              JOIN jobs j ON j.id = of.job_id
             WHERE of.group_id     = %s
               AND j.chunk_index   = %s
               AND of.frame_number IS NOT NULL
            """,
            (group_id, chunk_index),
        )
        return {int(r["frame_number"]) for r in rows}

    def frame_numbers_per_chunk_for_group(
        self, group_id: str,
    ) -> dict[int, set[int]]:
        """Bulk equivalent of ``{ci: frame_numbers_for_chunk(group_id, ci)
        for ...}`` covering every chunk_index in the group.  One query
        instead of N -- used by the render-groups detail page so it can
        compute per-chunk progress for every chunk in a single round-trip.

        Same JOIN through ``jobs`` as ``frame_numbers_for_chunk`` but
        without the per-chunk filter.
        """
        rows = query_all(
            "SELECT j.chunk_index, of.frame_number "
            "FROM output_frames of "
            "JOIN jobs j ON j.id = of.job_id "
            "WHERE of.group_id = %s AND of.frame_number IS NOT NULL",
            (group_id,),
        )
        out: dict[int, set[int]] = {}
        for r in rows:
            ci = r["chunk_index"] or 0
            out.setdefault(ci, set()).add(int(r["frame_number"]))
        return out

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
