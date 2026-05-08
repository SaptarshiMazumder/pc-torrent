"""Postgres reads for recent terminal Modal jobs whose
``modal_function_call_id`` we still hold.

Modal has no bulk "list active calls" API, so the backup monitor can't
detect orphaned Modal calls the way it can for Vast.  Instead it
inverts the strategy: scan local jobs that we believe are terminal,
re-cancel via the orchestrator.  Catches the failure mode where our
prior cancel didn't reach Modal (network blip, deploy mid-callback,
etc.).

Bounded by ``since_hours`` and ``limit`` so a misconfigured run can't
walk the whole jobs table.
"""

from __future__ import annotations

from typing import Any

import psycopg2.extras


_TERMINAL = ("done", "failed", "cancelled")


class ModalTerminalJobsRepository:

    def list_recent_terminal(
        self, db, *, since_hours: int, limit: int,
    ) -> list[dict[str, Any]]:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, status, modal_function_call_id, group_id, completed_at
                FROM jobs
                WHERE machine_type = 'modal_serverless'
                  AND modal_function_call_id IS NOT NULL
                  AND status = ANY(%s)
                  AND completed_at IS NOT NULL
                  AND completed_at > now() - (%s || ' hours')::interval
                ORDER BY completed_at DESC
                LIMIT %s
                """,
                (list(_TERMINAL), str(since_hours), limit),
            )
            return [dict(r) for r in cur.fetchall()]
