"""AllocationTargetValidator — Protocol for per-target eligibility checks.

Allocator strategies hold a list of TargetValidators and pass each
candidate target through every validator before considering it eligible.
A validator returning ``False`` removes the target from the eligible set.

Validators are PURE: no I/O, no global state, no FleetRegistry lookups.
Anything that needs runtime fleet info (enabled flag, in-flight count)
stays inside the strategy.  Validators are for per-target rules that
depend only on (target, context).
"""

from __future__ import annotations

from typing import Protocol

from serverV2.allocation.allocation_strategies.validators.allocation_validation_context import (
    AllocationValidationContext,
)


class AllocationTargetValidator(Protocol):

    def is_valid(self, target, context: AllocationValidationContext) -> bool:
        """Return True if ``target`` is eligible under this rule.

        ``target`` is either a ``CommunityMachine`` or a ``FleetCapability``
        — duck-typed.  Validators that only care about one type should
        return True for the other (don't filter what you don't understand).
        """
        ...
