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

# Command logging is ON by default (in-memory, zero Redis ops).  Set
# REDIS_CMD_LOG=0 to hand out the raw client with no proxy overhead.
_CMD_LOG_ENABLED = os.environ.get("REDIS_CMD_LOG", "1").strip() not in ("0", "false", "False", "")


def namespaced(key: str) -> str:
    """Prefix ``key`` with ``REDIS_KEY_PREFIX``.  Only call on fixed
    (non-id) keys; id-keyed keys are inherently env-safe via their UUID."""
    return f"{REDIS_KEY_PREFIX}{key}"


class RedisClient:

    def __init__(self, url: str | None = None) -> None:
        self._url = (url if url is not None else os.environ.get("REDIS_URL", "")).strip()
        self._redis: redis.Redis | None = None
        self._proxy = None
        self._activity = None

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
            self._install_command_logger()
        except Exception as exc:
            log.error("Redis connect failed (%s) — Redis disabled", exc)
            self._redis = None

    def _install_command_logger(self) -> None:
        if not _CMD_LOG_ENABLED or self._redis is None:
            return
        # Imported lazily so the proxy module is only loaded when enabled.
        from serverV2.infrastructure.redis_command_log import LoggingRedis, RedisActivity
        self._activity = RedisActivity(prefix=REDIS_KEY_PREFIX)
        self._proxy = LoggingRedis(self._redis, self._activity)
        log.info("Redis command logging enabled")

    def client(self) -> redis.Redis | None:
        # Hand out the logging proxy when enabled so every command from every
        # caller is recorded at the one choke point; raw client otherwise.
        if self._proxy is not None:
            return self._proxy
        return self._redis

    def activity(self):
        """The in-memory command recorder (None if logging disabled / Redis
        down).  Read by the admin dashboard's Redis panel."""
        return self._activity
