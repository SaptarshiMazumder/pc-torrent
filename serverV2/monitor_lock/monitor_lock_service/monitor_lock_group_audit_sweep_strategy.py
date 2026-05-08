"""MonitorLockGroupAuditSweepStrategy — periodic group-status drift audit.

Closes a class of bug nothing else catches: a group whose chunks have
all completed but whose ``render_groups.status`` is still ``running``
or ``pending`` because the orchestrator's ``reconcile_group_status``
call crashed mid-flight (instance OOM during the success pipeline,
network partition during a Redis write, etc.).

Every ~30 s (the sweeper's tick), iterate active render groups and
re-run ``reconcile_group_status`` against each, then call
``release_terminal_group_resources`` so any drained-but-not-cleaned
allocation queue rows for newly-terminal groups get drained on this
sweep too (drain belongs to the same recovery operation; running it
only inside the chunk-event pipelines would leave the audit's recovery
incomplete).  Both calls are idempotent -- no-ops in steady state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.repositories.render_group_repository import RenderGroupRepository

if TYPE_CHECKING:
    from serverV2.orchestrator.lifecycle import RenderLifecycle

log = logging.getLogger(__name__)


class MonitorLockGroupAuditSweepStrategy:

    def __init__(
        self,
        *,
        group_repo: RenderGroupRepository,
        lifecycle: "RenderLifecycle",
    ) -> None:
        self._group_repo = group_repo
        self._lifecycle = lifecycle

    def sweep(self) -> None:
        for group in self._group_repo.get_active_groups():
            group_id = group.get("id")
            if not group_id:
                continue
            try:
                self._lifecycle.reconcile_group_status(group_id)
                self._lifecycle.release_terminal_group_resources(group_id)
            except Exception:
                log.exception(
                    "GroupAudit reconcile failed for group %s", group_id,
                )
