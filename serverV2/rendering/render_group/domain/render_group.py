"""Domain: render-group invariants and legal state transitions.

The DATA shape lives in the shared kernel (``core.models.RenderGroup``).
This module owns the RULES about that shape -- which statuses are terminal,
which transitions are legal -- kept pure and free of I/O.  Use-cases ask
these guards before acting; they never hard-code status strings themselves.
"""

from __future__ import annotations

from serverV2.core.models import RenderGroup

# The three sinks.  Mirrors ``RenderGroup.is_terminal`` in the kernel; kept
# here as the domain-owned set the rollup and snapshot logic branch on.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def can_cancel(group: RenderGroup) -> bool:
    """A group may be cancelled only while it is still non-terminal."""
    raise NotImplementedError


def can_confirm_upload(group: RenderGroup) -> bool:
    """Upload may be confirmed only from the ``uploading`` state."""
    raise NotImplementedError
