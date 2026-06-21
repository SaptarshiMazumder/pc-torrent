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


# Per-env prefix for FIXED (non-id) Redis keys so multiple envs sharing
# one Redis cannot collide on singleton locks / aggregate keys.
# - dev sets REDIS_KEY_PREFIX=dev:
# - staging sets REDIS_KEY_PREFIX=staging:
# - prod / test leave it unset (empty string) -- existing keys unchanged.
# id-keyed keys like job:{job_id}:hb are NOT prefixed -- the backup
# monitor reads them directly and would stop seeing heartbeats.
REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "")


def namespaced(key: str) -> str:
    """Prefix ``key`` with ``REDIS_KEY_PREFIX``.  Only call on fixed
    (non-id) keys; id-keyed keys are inherently env-safe via their UUID."""
    return f"{REDIS_KEY_PREFIX}{key}"


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
