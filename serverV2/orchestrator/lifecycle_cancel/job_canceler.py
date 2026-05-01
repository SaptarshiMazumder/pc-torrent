"""JobCanceler — tear down one job's resources.

Single responsibility: the per-job cancel atom.  Each step is exposed
as its own method so ``RenderCanceler`` can interleave them across N
jobs in the order the multi-pass requires (mark all jobs terminal in
the DB before any provider call returns).  Callers that only need to
cancel one job (the B2 per-instance Cancel button via
``RenderLifecycle.cancel_one_job``) call ``cancel_one(raw)``, which
runs the four steps in the same order back-to-back.

Steps and why they happen in this order:

  1. ``mark_cancelled``  — flip ``status='cancelled'`` in the DB.
     This is the most important step.  Once the row is terminal,
     CallbackRouter's ``is_job_terminal`` short-circuit drops every
     subsequent failure signal for this job (provider monitor's
     last-tick race, agent's /agent-failure, backup_monitor sweep).
     No retry can fire after this point.
  2. ``stop_monitoring`` — set the per-job monitor's stop event so
     it doesn't issue any more on_failure callbacks of its own.
  3. ``release_ledger`` — free the in-progress chunk slot.  Belt
     and suspenders: if a delayed signal somehow slips past step 1's
     terminal guard, ``release_if_owner`` would return False on it
     (row already gone) and bail before the retry path.
  4. ``cancel_provider`` — destroy the Vast instance / cancel the
     Modal FunctionCall.  Slowest; does the actual provider RPC.
     Community is a no-op (no provider; the agent's
     /cancel-status poll observes step 1's terminal state on its
     next tick and stops the local render).
"""

from __future__ import annotations

import logging
from typing import Any

from serverV2.fleets.registry import FleetRegistry
from serverV2.repositories.in_progress_chunk_repository import InProgressChunkRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.machine_repository import MachineRepository

log = logging.getLogger(__name__)


class JobCanceler:

    def __init__(
        self,
        *,
        job_repo: JobRepository,
        machine_repo: MachineRepository,
        in_progress_repo: InProgressChunkRepository,
        fleet_registry: FleetRegistry,
    ) -> None:
        self._job_repo = job_repo
        self._machine_repo = machine_repo
        self._in_progress = in_progress_repo
        self._fleet = fleet_registry

    # ------------------------------------------------------------------
    # individual steps (RenderCanceler interleaves these across jobs)
    # ------------------------------------------------------------------

    def mark_cancelled(self, raw: dict[str, Any]) -> None:
        """Step 1.  Flip status to ``'cancelled'`` and release the
        community machine lock.  Most important step: the row's
        terminal status is what makes CallbackRouter drop all
        subsequent failure signals for this job.
        """
        self._job_repo.update_status(
            raw["id"], "cancelled", error="Cancelled by user",
        )
        machine_id = raw.get("machine_id")
        fleet = (raw.get("machine_type") or "").strip()
        if fleet == "windows" and machine_id:
            self._machine_repo.set_available(machine_id)

    def stop_monitoring(self, raw: dict[str, Any]) -> None:
        """Step 2.  Halt the per-job monitor thread (Vast/Modal).
        Community has no per-job monitor — this is a no-op there."""
        fleet = (raw.get("machine_type") or "").strip()
        strategy = self._fleet.get(fleet)
        if strategy is not None:
            strategy.stop_monitoring(raw["id"])

    def release_ledger(self, raw: dict[str, Any]) -> None:
        """Step 3.  Free the in-progress chunk slot for this job's
        (group_id, chunk_index).  No-op if the slot is already empty
        or owned by a different job (unlikely after step 1)."""
        group_id = raw.get("group_id") or ""
        chunk_index = raw.get("chunk_index") or 0
        if group_id:
            self._in_progress.release(group_id, chunk_index)

    def cancel_provider(self, raw: dict[str, Any]) -> None:
        """Step 4.  Tell the provider to destroy the container or
        cancel the function call.  Best-effort: failures are logged
        but not raised — the row is already terminal in the DB and
        the monitor is stopped, so even if the provider call fails
        we don't lose correctness, only money (until the provider's
        own idle timer reaps the instance)."""
        job_id = raw["id"]
        fleet = (raw.get("machine_type") or "").strip()
        strategy = self._fleet.get(fleet)
        if strategy is None:
            log.warning(
                "cancel %s: no strategy for fleet=%r — provider job not cancelled",
                job_id, fleet,
            )
            return
        if not strategy.is_enabled():
            log.warning(
                "cancel %s: fleet %s disabled — provider job not cancelled",
                job_id, fleet,
            )
            return
        pid = strategy.provider_job_id_from_job(raw)
        if not pid:
            log.warning(
                "cancel %s: fleet=%s has no provider_job_id stored — cannot cancel provider-side",
                job_id, fleet,
            )
            return
        try:
            strategy.cancel(pid)
            log.info(
                "cancel %s: fleet=%s pid=%s — cancel call returned",
                job_id, fleet, pid,
            )
        except Exception as exc:
            log.warning(
                "cancel %s: fleet=%s pid=%s raised %s: %s",
                job_id, fleet, pid, type(exc).__name__, exc,
            )

    # ------------------------------------------------------------------
    # convenience: all four steps for one job
    # ------------------------------------------------------------------

    def cancel_one(self, raw: dict[str, Any]) -> None:
        """Run all four steps in order for a single job.  Used by the
        B2 per-instance cancel path (``POST /jobs/{id}/cancel``).  The
        group-level cancel does NOT call this -- it interleaves the
        steps across N jobs to preserve the all-jobs-terminal-before-
        any-provider-call invariant."""
        self.mark_cancelled(raw)
        self.stop_monitoring(raw)
        self.release_ledger(raw)
        self.cancel_provider(raw)
