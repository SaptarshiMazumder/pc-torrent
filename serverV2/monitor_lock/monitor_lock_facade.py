"""MonitorLockFacade — public surface of the monitor_lock module.

Owns the sweep daemon's lifecycle.  Bootstrap and main.py talk to
this; the sweeper / strategies behind it are internal to the service
layer and never imported from outside the module.

The Redis lock primitive (``MonitorLockRepository``) is also exported
from the module's ``__init__`` because it's used directly by the fleet
monitors that hold the singleton locks.

``MonitorLockFacade.build(...)`` is the composition entry point.  It
takes the high-level fleet refs (the three fleet singletons + the
render lifecycle for group audit) and assembles four sweep strategies
+ the sweeper internally, so bootstrap never imports the service layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from serverV2.monitor_lock.monitor_lock_service.monitor_lock_community_sweep_strategy import (
    MonitorLockCommunitySweepStrategy,
)
from serverV2.monitor_lock.monitor_lock_service.monitor_lock_group_audit_sweep_strategy import (
    MonitorLockGroupAuditSweepStrategy,
)
from serverV2.monitor_lock.monitor_lock_service.monitor_lock_modal_sweep_strategy import (
    MonitorLockModalSweepStrategy,
)
from serverV2.monitor_lock.monitor_lock_service.monitor_lock_sweeper import (
    MonitorLockSweeper,
)
from serverV2.monitor_lock.monitor_lock_service.monitor_lock_vast_sweep_strategy import (
    MonitorLockVastSweepStrategy,
)

if TYPE_CHECKING:
    from serverV2.monitor_lock.monitor_lock_daemon import MonitorLockDaemon
    from serverV2.orchestrator.lifecycle import RenderLifecycle
    from serverV2.repositories.render_group_repository import RenderGroupRepository


class MonitorLockFacade:

    def __init__(self, *, sweeper: MonitorLockSweeper) -> None:
        self._sweeper = sweeper

    @classmethod
    def build(
        cls,
        *,
        vast_fleet_monitor: "MonitorLockDaemon",
        modal_fleet_monitor: "MonitorLockDaemon",
        community_monitor: "MonitorLockDaemon",
        group_repo: "RenderGroupRepository",
        lifecycle: "RenderLifecycle",
        interval_sec: int = 30,
    ) -> "MonitorLockFacade":
        """Compose the strategies + sweeper for this process.  The only
        constructor bootstrap needs to know.

        The three fleet singletons are typed as ``MonitorLockDaemon``
        rather than concrete classes -- this module owns the contract
        for what counts as a supervised singleton.
        """
        strategies = [
            MonitorLockVastSweepStrategy(
                vast_fleet_monitor=vast_fleet_monitor,
            ),
            MonitorLockModalSweepStrategy(
                modal_fleet_monitor=modal_fleet_monitor,
            ),
            MonitorLockCommunitySweepStrategy(
                community_monitor=community_monitor,
            ),
            MonitorLockGroupAuditSweepStrategy(
                group_repo=group_repo,
                lifecycle=lifecycle,
            ),
        ]
        sweeper = MonitorLockSweeper(
            strategies=strategies, interval_sec=interval_sec,
        )
        return cls(sweeper=sweeper)

    def start(self) -> None:
        """Begin the per-tick sweep on this instance.  Idempotent."""
        self._sweeper.start()

    def stop(self) -> None:
        """Halt the sweep loop.  Best-effort -- the daemon thread is
        a daemon and will also die with the process."""
        self._sweeper.stop()
