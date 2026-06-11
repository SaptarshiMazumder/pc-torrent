"""Users router — /me profile read + update."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.user_profile import UpdateProfilePayload, UserProfileResponse
from serverV2.users import UserFacade

router = APIRouter(tags=["users"])

_users: UserFacade | None = None


def init(user_facade: UserFacade) -> None:
    global _users
    _users = user_facade


def _facade() -> UserFacade:
    if _users is None:
        raise HTTPException(500, "UserFacade not initialized")
    return _users


@router.get("/me", response_model=UserProfileResponse)
def get_profile(user: dict = Depends(get_current_user)):
    facade = _facade()
    profile = facade.get_profile(user["uid"], user.get("email"))
    return UserProfileResponse(
        uid=user["uid"],
        email=profile.get("email", "") or "",
        display_name=profile.get("display_name", "") or "",
        tier=profile.get("tier", "free") or "free",
        credits=float(profile.get("credits", 0.0) or 0.0),
        credits_per_usd=facade.credits_per_usd,
    )


@router.put("/me")
def update_profile(
    payload: UpdateProfilePayload, user: dict = Depends(get_current_user),
):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    _facade().update_profile(user["uid"], updates)
    return {"success": True}
