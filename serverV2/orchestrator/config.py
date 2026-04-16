"""Orchestration config — all dispatch/retry/cancel tunables live here."""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_RETRIES = _env_int("ORCHESTRATOR_MAX_RETRIES", 0)
