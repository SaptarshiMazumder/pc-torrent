"""Postgres reads keyed by Vast instance id, used by the ghost scanner
to match provider-side instances against local job rows.

Stateless query object -- the connection is passed per call so the
scanner can recycle connections per tick (dodges stale TLS).
"""

from __future__ import annotations

from typing import Any

import psycopg2.extras


class VastJobsRepository:

    def get_by_vast_ids(
        self, db, vast_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        """Bulk-look up local jobs by their stored ``vast_job_id``.

        Returns a ``{vast_id: row}`` dict so the scanner can resolve
        every provider instance with one round trip.  Missing ids
        simply don't appear in the dict.

        ``vast_job_id`` is stored as TEXT but holds the integer Vast
        instance id; cast both sides to int for the comparison.
        """
        if not vast_ids:
            return {}
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, status, vast_job_id, group_id
                FROM jobs
                WHERE machine_type = 'vast_serverless'
                  AND vast_job_id IS NOT NULL
                  AND vast_job_id::bigint = ANY(%s)
                """,
                (vast_ids,),
            )
            rows = cur.fetchall()
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                key = int(row["vast_job_id"])
            except (TypeError, ValueError):
                continue
            out[key] = dict(row)
        return out
