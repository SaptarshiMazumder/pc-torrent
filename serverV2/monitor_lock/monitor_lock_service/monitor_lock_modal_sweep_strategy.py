"""MonitorLockModalSweepStrategy — claim the Modal singleton lock.

Same shape as Vast and community strategies: per tick, call
``try_start()`` on the singleton.  The lock acquire inside decides
whether THIS Cloud Run instance now runs the Modal fleet scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.fleets.modal.monitor.modal_fleet_monitor import ModalFleetMonitor


class MonitorLockModalSweepStrategy:

    def __init__(self, *, modal_fleet_monitor: "ModalFleetMonitor") -> None:
        self._fleet_monitor = modal_fleet_monitor

    def sweep(self) -> None:
        self._fleet_monitor.try_start()
