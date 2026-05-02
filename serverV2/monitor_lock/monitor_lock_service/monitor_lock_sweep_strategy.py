"""MonitorLockSweepStrategy — per-fleet "claim any unowned monitors" contract.

The sweeper is fleet-agnostic: it loops at a fixed interval and asks
every registered strategy to do its sweep().  Each strategy owns the
fleet-specific knowledge -- which DB columns identify an active job,
how to construct the blend URL, whether the fleet is enabled, and which
manager / monitor to delegate the actual ``start_monitoring`` to.

This is the only Strategy-pattern split that earns its keep here: the
three fleets share an identical ``per-tick claim`` shape but differ in
*everything inside it*.  Adding a fourth fleet (RunPod / Lambda Labs /
etc.) is a single new strategy class -- the sweeper itself does not
change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MonitorLockSweepStrategy(Protocol):

    def sweep(self) -> None:
        """Reclaim any unowned monitors for this fleet.

        Implementations MUST be idempotent: the sweeper calls this
        every tick on every instance.  The Redis lock acquired by the
        underlying ``start_monitoring`` / ``try_start`` is what enforces
        single-owner; the strategy's job is just to drive that path
        for any DB row that looks active.

        MUST NOT raise.  The sweeper logs and continues if a single
        strategy throws; one fleet's failure must not stall the others.
        """
        ...
