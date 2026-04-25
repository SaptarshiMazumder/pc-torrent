"""ValidationContext — per-allocation context passed to every TargetValidator.

Carries the request-scope inputs that validators may need to make a decision.
Frozen so a single instance is safely shared across N validator checks.
Extend with new fields as new validator rules emerge (tier, price cap, ...).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationContext:
    engine: str | None = None
