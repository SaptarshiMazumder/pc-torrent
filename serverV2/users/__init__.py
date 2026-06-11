"""Users module -- user profile reads/writes and credit accounting.

Public surface is ``UserFacade``; consumers should depend only on it.
Internal collaborators ``UserService`` and ``UserProfileRepository``
are exported so bootstrap can wire the dependency graph.
"""

from serverV2.users.user_facade import UserFacade
from serverV2.users.user_profile_repository import UserProfileRepository
from serverV2.users.user_service import UserService

__all__ = [
    "UserFacade",
    "UserProfileRepository",
    "UserService",
]
