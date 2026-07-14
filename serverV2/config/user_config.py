"""UserConfig — tunable user-account settings sourced from Firestore.

Currently a single knob: the credit grant applied to a brand-new user
profile.  Kept separate from ``RenderConfig`` (render / allocation
tuning) so the two concerns evolve independently.

The grant value lives ONLY in the Firestore ``config/user_config`` doc.
An absent doc or field yields ``0.0`` -- no grant, no hard-coded
default -- so turning the grant on is purely a Firestore edit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserConfig:
    signup_grant_credits: float

    @classmethod
    def from_dict(cls, d: dict) -> "UserConfig":
        raw = d.get("signup_grant_credits")
        return cls(signup_grant_credits=float(raw) if raw is not None else 0.0)
