"""FastAPI dependencies — thin wrappers that delegate to infrastructure."""

from __future__ import annotations

from typing import Any

from fastapi import Depends

from serverV2.infrastructure.auth.token_verifier import get_current_user

__all__ = ["get_current_user"]
