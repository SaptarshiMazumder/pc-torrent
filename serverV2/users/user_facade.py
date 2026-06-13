"""UserFacade — public entry point for the users module.

Composes ``UserProfileRepository`` + ``UserService`` and exposes the
single surface that callers depend on.  Routers, services in other
modules, and the orchestrator-side ``UsersClient`` all go through this
class so the module is free to evolve its internals.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.user_role import UserRole
from serverV2.users.user_profile_repository import UserProfileRepository
from serverV2.users.user_service import UserService


class UserFacade:

    def __init__(
        self,
        *,
        repository: UserProfileRepository,
        service: UserService,
    ) -> None:
        self._repository = repository
        self._service = service

    @property
    def credits_per_usd(self) -> float:
        """Conversion rate exposed for client display.  Read-only."""
        return self._service.credits_per_usd

    def get_profile(
        self, uid: str, email: str | None = None,
    ) -> dict[str, Any]:
        return self._repository.create_if_missing(uid, email)

    def update_profile(self, uid: str, updates: dict[str, Any]) -> None:
        self._repository.update(uid, updates)

    def get_role(self, uid: str) -> UserRole:
        """Authorization role for ``uid`` -- defaults to USER on absent data."""
        return self._repository.get_role(uid)

    def usd_to_credits(self, total_cost_usd: float) -> float:
        """USD to credits using the configured rate.  Stateless."""
        return self._service.compute_credit_debit(total_cost_usd)

    def record_spend(
        self, uid: str, task_id: str, total_credits_spent: float,
    ) -> float:
        """Set the running spend total for one task.

        High-water mark: each call computes ``delta = max(0, new - existing)``
        inside the Firestore transaction and debits only the delta from
        ``users/{uid}.credits``.  Returns the delta applied (>= 0).
        Lower values than the existing total are no-ops -- never refunds.
        """
        return self._repository.record_spend(uid, task_id, total_credits_spent)
