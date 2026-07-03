"""DownloadStatsRepository — Redis counters for artifact downloads.

One HASH, ``stats:downloads`` (namespaced), two field shapes:

  * ``{kind}``            -> lifetime count
  * ``{kind}:{yyyymmdd}`` -> per-day count

``kind`` is a short label stamped by the download endpoints
(``agent_windows``, ``agent_linux``, ``job_zip``, ``group_zip``).
Fail-open like every other Redis repo here: a Redis blip silently
drops the increment — download stats are best-effort observability,
never worth failing a download over.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis

from serverV2.infrastructure.redis_client import RedisClient, namespaced

log = logging.getLogger(__name__)

_KEY = "stats:downloads"


class DownloadStatsRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    @staticmethod
    def _key() -> str:
        return namespaced(_KEY)

    def record(self, kind: str) -> None:
        if not kind:
            return
        client = self._redis_client.client()
        if client is None:
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        try:
            pipe = client.pipeline(transaction=False)
            pipe.hincrby(self._key(), kind, 1)
            pipe.hincrby(self._key(), f"{kind}:{day}", 1)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("DownloadStats record(%s) failed: %s", kind, exc)

    def totals(self) -> dict[str, dict]:
        """``{kind: {"total": N, "by_day": {yyyymmdd: N}}}``.  Empty dict
        on Redis error / no entries."""
        client = self._redis_client.client()
        if client is None:
            return {}
        try:
            raw = client.hgetall(self._key())
        except redis.RedisError as exc:
            log.warning("DownloadStats totals failed: %s", exc)
            return {}
        out: dict[str, dict] = {}
        for field, value in (raw or {}).items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            kind, sep, day = field.rpartition(":")
            if sep and day.isdigit() and len(day) == 8:
                out.setdefault(kind, {"total": 0, "by_day": {}})["by_day"][day] = count
            else:
                out.setdefault(field, {"total": 0, "by_day": {}})["total"] = count
        return out
