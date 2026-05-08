"""Backup monitor configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BackupMonitorConfig:
    database_url: str
    redis_url: str
    orchestrator_url: str
    orphan_secret: str
    http_timeout_sec: int
    # --- ghost-instance sweep (Vast) ---
    vast_api_key: str
    vast_ghost_min_age_sec: float
    # --- terminal-cleanup retry (Modal) ---
    modal_terminal_window_hours: int
    modal_terminal_max_rows: int

    @classmethod
    def from_env(cls) -> "BackupMonitorConfig":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            redis_url=os.environ["REDIS_URL"],
            orchestrator_url=os.environ["ORCHESTRATOR_URL"].rstrip("/"),
            orphan_secret=os.environ["ORPHAN_SECRET"],
            http_timeout_sec=int(os.environ.get("HTTP_TIMEOUT_SEC", "10")),
            vast_api_key=os.environ["VAST_API_KEY"],
            vast_ghost_min_age_sec=float(
                os.environ.get("VAST_GHOST_MIN_AGE_SEC", "120"),
            ),
            modal_terminal_window_hours=int(
                os.environ.get("MODAL_TERMINAL_WINDOW_HOURS", "24"),
            ),
            modal_terminal_max_rows=int(
                os.environ.get("MODAL_TERMINAL_MAX_ROWS", "200"),
            ),
        )
