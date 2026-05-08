"""MonitorLockVastSweepStrategy — claim the Vast singleton lock.

Same shape as the community strategy: per tick, call ``try_start()``
on the singleton.  The lock acquire inside that method decides whether
THIS Cloud Run instance now runs the Vast fleet scan.

Replaces the previous "iterate active rows and call start_monitoring
on each" approach — there are no per-job threads to reclaim anymore;
the singleton itself IS the reclaim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serverV2.fleets.vast.monitor.vast_fleet_monitor import VastFleetMonitor


class MonitorLockVastSweepStrategy:

    def __init__(self, *, vast_fleet_monitor: "VastFleetMonitor") -> None:
        self._fleet_monitor = vast_fleet_monitor

    def sweep(self) -> None:
        self._fleet_monitor.try_start()
