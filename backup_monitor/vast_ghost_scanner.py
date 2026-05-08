"""Per-tick scanner that finds Vast.ai ghost instances and reports
them to the orchestrator.

Definition of ghost (the strict criterion):

  * The Vast instance has been running for longer than the
    ``min_instance_age_sec`` grace window AND
  * Either no local job row points to it, OR the local job row's
    status is terminal (``done``/``failed``/``cancelled``).

The age guard prevents racing a fresh dispatch: a new instance is
created on Vast a few seconds before the dispatcher writes
``vast_job_id`` to the jobs table.  Without the guard, a sweep tick
landing in that window would wrongly classify the in-flight
dispatch as orphaned.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import psycopg2.extensions

from ghost_reporter import GhostReporter
from vast_api_client import VastApiClient
from vast_jobs_repository import VastJobsRepository

log = logging.getLogger(__name__)

_TERMINAL = frozenset({"done", "failed", "cancelled"})


class VastGhostScanner:

    def __init__(
        self,
        *,
        vast_api: VastApiClient,
        vast_jobs_repo: VastJobsRepository,
        reporter: GhostReporter,
        min_instance_age_sec: float,
        db_factory: Callable[[], psycopg2.extensions.connection],
    ) -> None:
        self._vast_api = vast_api
        self._vast_jobs_repo = vast_jobs_repo
        self._reporter = reporter
        self._min_age = float(min_instance_age_sec)
        self._db_factory = db_factory

    def tick(self) -> None:
        provider = self._vast_api.list_instances()
        if not provider:
            log.info("Vast ghost scan: 0 instances on account, nothing to check")
            return

        provider_ids: list[int] = []
        for inst in provider:
            try:
                provider_ids.append(int(inst["id"]))
            except (KeyError, TypeError, ValueError):
                continue

        db = self._db_factory()
        try:
            local = self._vast_jobs_repo.get_by_vast_ids(db, provider_ids)
        finally:
            try:
                db.close()
            except Exception:
                pass

        now = time.time()
        ghosts = 0
        for inst in provider:
            try:
                instance_id = int(inst["id"])
            except (KeyError, TypeError, ValueError):
                continue
            row = local.get(instance_id)
            reason = self._classify(inst, row, now)
            if reason is None:
                continue
            log.info(
                "Vast ghost detected: instance=%s reason=%s",
                instance_id, reason,
            )
            self._reporter.report_vast_ghost(instance_id, reason=reason)
            ghosts += 1

        log.info(
            "Vast ghost scan: %d ghost(s) in %d provider instance(s)",
            ghosts, len(provider),
        )

    def _classify(
        self,
        instance: dict,
        local_row: dict | None,
        now: float,
    ) -> str | None:
        # If a local row exists and its status is terminal, the provider
        # instance is leaked regardless of age -- always a ghost.
        if local_row is not None:
            status = str(local_row.get("status") or "")
            if status in _TERMINAL:
                return f"local_row_terminal({status})"
            return None

        # No local row -- only flag once the instance has aged past the
        # dispatch-write race window.
        age_sec = self._instance_age_sec(instance, now)
        if age_sec is None:
            # Couldn't compute age -- safer to skip than false-positive.
            return None
        if age_sec >= self._min_age:
            return f"no_local_row(age_sec={int(age_sec)})"
        return None

    @staticmethod
    def _instance_age_sec(instance: dict, now: float) -> float | None:
        # Vast returns ``start_date`` as a Unix timestamp (seconds since
        # epoch) for active rentals.  Fall back to None if it's missing
        # so the caller skips rather than guessing.
        start = instance.get("start_date")
        if start is None:
            return None
        try:
            return max(0.0, now - float(start))
        except (TypeError, ValueError):
            return None
