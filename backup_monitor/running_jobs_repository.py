"""Postgres reads for running serverless jobs that the backup monitor scans."""

from __future__ import annotations

from typing import Any

import psycopg2.extras

SERVERLESS_FLEETS = ("vast_serverless", "modal_serverless")


class RunningJobsRepository:
    """Stateless query object — connection is passed in per call so the
    scanner can recycle connections per tick (dodges stale TLS).
    """

    def find_running_serverless(self, db) -> list[dict[str, Any]]:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, machine_type, group_id
                FROM jobs
                WHERE status = 'running'
                  AND machine_type = ANY(%s)
                """,
                (list(SERVERLESS_FLEETS),),
            )
            return [dict(r) for r in cur.fetchall()]
