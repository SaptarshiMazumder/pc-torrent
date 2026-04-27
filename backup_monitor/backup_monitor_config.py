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

    @classmethod
    def from_env(cls) -> "BackupMonitorConfig":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            redis_url=os.environ["REDIS_URL"],
            orchestrator_url=os.environ["ORCHESTRATOR_URL"].rstrip("/"),
            orphan_secret=os.environ["ORPHAN_SECRET"],
            http_timeout_sec=int(os.environ.get("HTTP_TIMEOUT_SEC", "10")),
        )
