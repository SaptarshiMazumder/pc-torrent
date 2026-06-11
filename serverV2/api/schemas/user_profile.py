"""User profile API schemas — request + response models for /me routes."""

from __future__ import annotations

from pydantic import BaseModel


class UpdateProfilePayload(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    tier: str | None = None


class UserProfileResponse(BaseModel):
    uid: str
    email: str
    display_name: str
    tier: str
    credits: float
    credits_per_usd: float
