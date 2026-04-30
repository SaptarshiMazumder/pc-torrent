"""StallReason — value object for a positive stall verdict."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StallReason:
    rule: str
    message: str
