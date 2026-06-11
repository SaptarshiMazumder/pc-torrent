"""UsersClient -- orchestrator-side gateway to the users module.

The ONLY thing in the entire codebase allowed to import ``UserFacade``
outside the ``users`` module itself.  Inside orchestrator, the
``RenderOrchestrator`` facade reaches this client; no other layer.

Owns the recipe "given a job row, compute what the chunk has cost so
far and tell the users module to record that as the running spend
total" so ``RenderLifecycle`` stays focused on state mutation and the
orchestrator facade stays a thin shim.

Two entry points:
  * ``update_spend_for_chunk(row)`` -- fast path.  Caller already has
    the row (every fleet monitor's main tick does), so no repository
    read happens here.  ``row`` must include
    ``id`` / ``owner_uid`` / ``status`` / ``started_at`` /
    ``completed_at`` / ``price_per_hour_at_dispatch``.
  * ``update_spend_for_chunk_by_id(job_id)`` -- convenience used by
    the terminal-flip code paths in the orchestrator facade where
    only the job_id is known.  Does one read + delegates.

Converges by design: monotonically advances ``users/{uid}/spend/{task}
.total_credits_spent`` toward the live cost-so-far.  The marker
defaults to zero, so the first call charges the full live cost; each
subsequent call charges only the delta since last call.
"""

from __future__ import annotations

import logging
from typing import Any

from serverV2.orchestrator.task_actual_cost import TaskActualCost
from serverV2.repositories.job_repository import JobRepository
from serverV2.users import UserFacade

log = logging.getLogger(__name__)


class UsersClient:

    def __init__(
        self,
        *,
        facade: UserFacade,
        job_repo: JobRepository,
        cost: TaskActualCost,
    ) -> None:
        self._facade = facade
        self._job_repo = job_repo
        self._cost = cost

    def update_spend_for_chunk(self, row: dict[str, Any]) -> float:
        """Bring the user's recorded spend for this chunk up to its
        current cost-so-far.  ``row`` must already carry ``owner_uid``
        plus the cost-formula inputs -- caller is the monitor's tick
        loop, which has all of those from its bulk SELECT.

        Returns the credit delta actually applied (>= 0).  Wraps in
        try/except so a billing failure never breaks the calling
        monitor / lifecycle path.
        """
        try:
            uid = row.get("owner_uid")
            if not uid:
                return 0.0
            job_id = row.get("id")
            if not job_id:
                return 0.0
            _, total_cost_usd = self._cost.compute(
                started_at=row.get("started_at"),
                completed_at=row.get("completed_at"),
                price_per_hour=row.get("price_per_hour_at_dispatch"),
                status=str(row.get("status") or ""),
            )
            if not total_cost_usd or total_cost_usd <= 0:
                return 0.0
            total_credits_spent = self._facade.usd_to_credits(total_cost_usd)
            return self._facade.record_spend(
                str(uid), str(job_id), total_credits_spent,
            )
        except Exception as exc:
            log.warning(
                "UsersClient spend update failed for job %s: %s",
                row.get("id"), exc,
            )
            return 0.0

    def update_spend_for_chunk_by_id(self, job_id: str) -> float:
        """Convenience: read the row (with owner) and delegate.  Used by
        the orchestrator facade's terminal-flip paths where ``completed_at``
        was just stamped and the spend marker needs the final value
        sealed.  One repository call -- not on the hot tick path.
        """
        raw = self._job_repo.get_raw_with_owner(job_id)
        if raw is None:
            return 0.0
        return self.update_spend_for_chunk(raw)

    def update_spend_for_group_jobs(self, group_id: str) -> int:
        """Run ``update_spend_for_chunk`` against every job in a group.

        Used after ``cancel_render`` so any in-flight chunk that was
        ticking during the cancel gets its final spend sealed.  Pending
        chunks (no ``started_at``) produce a zero-cost no-op.  Already
        terminal chunks whose marker is at the final number produce a
        zero-delta no-op.

        Single bulk read with owner -- no per-row repository call.
        Returns the count of debits that actually applied a non-zero
        delta.
        """
        rows = self._job_repo.get_raw_by_group_with_owner(group_id)
        count = 0
        for row in rows:
            if self.update_spend_for_chunk(row) > 0:
                count += 1
        return count
