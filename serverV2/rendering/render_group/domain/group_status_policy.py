"""Domain rule: derive a render group's status from its chunk states.

Pure decision -- no I/O.  Given the current group status, the chunk
statuses, frame totals and whether allocation still has work in flight,
decide the group's rolled-up status and whether it changed enough to
persist.  This is the single source of truth for the chunk -> group
projection, and the reason a worker never sets ``group.status`` directly.

The return type (``GroupStatusResult``) already lives in the shared kernel.

PARITY NOTE: the body is intentionally left unimplemented in this skeleton.
The live rule is ``callbacks.group_status_aggregator.compute_group_status``.
Before this module is wired in, port that logic here and prove parity with
a table test against the old function -- do not re-derive it from memory.
"""

from __future__ import annotations

from serverV2.core.models import GroupStatusResult


def compute_group_status(
    *,
    current_group_status: str,
    job_statuses: list[str],
    total_frames: int,
    total_rendered: int,
    has_pending_allocation: bool,
) -> GroupStatusResult:
    raise NotImplementedError
