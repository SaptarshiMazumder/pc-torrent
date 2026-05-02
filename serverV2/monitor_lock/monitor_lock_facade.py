"""MonitorLockFacade — public surface of the monitor_lock module.

Owns the sweep daemon's lifecycle.  Bootstrap and main.py talk to
this; the sweeper / strategies behind it are internal to the service
layer and never imported from outside the module.

The Redis lock primitive (``MonitorLockRepository``) is also exported
from the module's ``__init__`` because it's used directly by the
fleet monitor managers and the per-tick monitor classes -- the lock
is part of the module's public surface alongside the facade.

``MonitorLockFacade.build(...)`` is the composition entry point.  It
takes the high-level fleet refs (configs, monitor managers, the
community monitor) and assembles the three sweep strategies + the
sweeper internally, so bootstrap never imports the service layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from serverV2.config import ModalConfig, VastConfig
from serverV2.monitor_lock.monitor_lock_service.monitor_lock_community_sweep_strategy import (
    MonitorLockCommunitySweepStrategy,
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
    from serverV2.fleets.community.community_monitor import CommunityMonitor
    from serverV2.fleets.modal.monitor import ModalMonitorManager
    from serverV2.fleets.vast.monitor import VastMonitorManager


class MonitorLockFacade:

    def __init__(self, *, sweeper: MonitorLockSweeper) -> None:
        self._sweeper = sweeper

    @classmethod
    def build(
        cls,
        *,
        vast_cfg: VastConfig,
        modal_cfg: ModalConfig,
        vast_manager: "VastMonitorManager",
        modal_manager: "ModalMonitorManager",
        community_monitor: "CommunityMonitor",
        interval_sec: int = 30,
    ) -> "MonitorLockFacade":
        """Compose the strategies + sweeper for this process.  The only
        constructor bootstrap needs to know."""
        strategies = [
            MonitorLockVastSweepStrategy(
                vast_cfg=vast_cfg, vast_manager=vast_manager,
            ),
            MonitorLockModalSweepStrategy(
                modal_cfg=modal_cfg, modal_manager=modal_manager,
            ),
            MonitorLockCommunitySweepStrategy(
                community_monitor=community_monitor,
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
