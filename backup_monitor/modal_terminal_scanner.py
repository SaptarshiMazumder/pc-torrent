"""Per-tick scanner that finds local Modal jobs in a terminal state
whose ``modal_function_call_id`` we still hold, and asks the
orchestrator to (re-)cancel them.

This is NOT ghost detection in the Vast sense -- the Modal SDK has no
bulk "list active calls" API, so the backup monitor can't enumerate
the provider side.  Instead it inverts the strategy: trust our own
DB and ask the orchestrator to retry cancellation for any terminal
row that still has a function-call id on file.  The cancel itself is
idempotent on Modal's side, so this is safe to run on every tick.

Bounded by ``window_hours`` and ``max_rows`` so a misconfigured run
can't flood the orchestrator.
"""

from __future__ import annotations

import logging
from typing import Callable

import psycopg2.extensions

from ghost_reporter import GhostReporter
from modal_terminal_jobs_repository import ModalTerminalJobsRepository

log = logging.getLogger(__name__)


class ModalTerminalScanner:

    def __init__(
        self,
        *,
        modal_terminal_jobs_repo: ModalTerminalJobsRepository,
        reporter: GhostReporter,
        window_hours: int,
        max_rows: int,
        db_factory: Callable[[], psycopg2.extensions.connection],
    ) -> None:
        self._repo = modal_terminal_jobs_repo
        self._reporter = reporter
        self._window_hours = int(window_hours)
        self._max_rows = int(max_rows)
        self._db_factory = db_factory

    def tick(self) -> None:
        db = self._db_factory()
        try:
            rows = self._repo.list_recent_terminal(
                db,
                since_hours=self._window_hours,
                limit=self._max_rows,
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

        for row in rows:
            call_id = row.get("modal_function_call_id")
            status = row.get("status") or ""
            if not call_id:
                continue
            log.info(
                "Modal terminal-cleanup: job=%s call=%s status=%s",
                row.get("id"), call_id, status,
            )
            self._reporter.report_modal_ghost(
                call_id, reason=f"local_row_terminal({status})",
            )

        log.info(
            "Modal terminal-cleanup scan: %d row(s) reported (window=%dh, limit=%d)",
            len(rows), self._window_hours, self._max_rows,
        )
