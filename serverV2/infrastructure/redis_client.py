"""Redis connection — thin wrapper around redis-py.

Zero domain knowledge.  Exposes a single ``redis.Redis`` instance that
repositories use.  Returns None from ``client()`` when REDIS_URL is unset
or the connection check failed, so callers can degrade gracefully.
"""

from __future__ import annotations

import logging
import os

import redis

log = logging.getLogger(__name__)


class RedisClient:

    def __init__(self, url: str | None = None) -> None:
        self._url = (url if url is not None else os.environ.get("REDIS_URL", "")).strip()
        self._redis: redis.Redis | None = None

    def connect(self) -> None:
        if not self._url:
            log.warning("REDIS_URL not set — Redis disabled")
            return
        try:
            self._redis = redis.Redis.from_url(
                self._url,
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            self._redis.ping()
            log.info("Redis connected")
        except Exception as exc:
            log.error("Redis connect failed (%s) — Redis disabled", exc)
            self._redis = None

    def client(self) -> redis.Redis | None:
        return self._redis
