"""RenderConfigRedisMirror -- Redis cache in front of the Firestore-
backed ``config/global`` document.

The document changes only via the admin UI (typically a couple of
edits per week).  Without a mirror, every monitor tick + dispatcher
tick + per-request resolver call would hit Firestore -- ~3 monitors
x 6 ticks/min on each instance easily blows past the Spark plan's
50K reads/day per env.

This sits as a sibling of ``RenderConfigRepository`` and is composed
into it via constructor injection.  Caching is opaque to callers:
``RenderConfigRepository.get()`` still returns a fresh-looking
``RenderConfig``; it just doesn't hit Firestore most of the time.

Cache shape:
  * Key: ``namespaced("config:render")`` -- env-prefixed so dev /
    staging / prod don't collide when sharing one Upstash.
  * Value: raw JSON dict (what's stored in the Firestore ``json``
    field).  Storing the dict (not the parsed ``RenderConfig``)
    keeps this layer engine-stupid -- if RenderConfig's shape
    changes, the mirror doesn't.
  * TTL: ``_CACHE_TTL_SEC`` (default 30s).  Admin edits invalidate
    the key immediately via ``invalidate()``; otherwise an edit
    propagates within one TTL window.

Fail-open: any Redis error logs and returns ``None`` -- the
repository then falls through to Firestore, same as today.  Redis
being unreachable never breaks config reads, just makes them slow.
"""

from __future__ import annotations

import json
import logging

from serverV2.infrastructure.redis_client import RedisClient, namespaced

log = logging.getLogger(__name__)


_CACHE_KEY = "config:render"
_CACHE_TTL_SEC = 30


class RenderConfigRedisMirror:

    def __init__(
        self,
        *,
        redis_client: RedisClient,
        ttl_sec: int = _CACHE_TTL_SEC,
    ) -> None:
        self._redis = redis_client
        self._ttl = int(ttl_sec)

    # ------------------------------------------------------------------

    def get_cached_dict(self) -> dict | None:
        """Read the cached raw config dict.  Returns None on cache
        miss, Redis disabled, parse error, or any Redis exception --
        callers should treat None as "fetch from Firestore"."""
        client = self._redis.client()
        if client is None:
            return None
        key = namespaced(_CACHE_KEY)
        try:
            raw = client.get(key)
        except Exception as exc:
            log.warning("RenderConfigRedisMirror get failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            log.warning(
                "RenderConfigRedisMirror cached value at %s is not valid JSON: %s",
                key, exc,
            )
            return None

    def set_cached_dict(self, value: dict) -> None:
        """Write the dict to Redis with TTL.  Silent on Redis errors --
        a failed write just means the next read goes to Firestore."""
        client = self._redis.client()
        if client is None:
            return
        key = namespaced(_CACHE_KEY)
        try:
            client.set(key, json.dumps(value), ex=self._ttl)
        except Exception as exc:
            log.warning("RenderConfigRedisMirror set failed: %s", exc)

    def invalidate(self) -> None:
        """Delete the cached key.  Called after ``admin_put`` so admin
        edits are visible on the next read instead of waiting up to
        one TTL window."""
        client = self._redis.client()
        if client is None:
            return
        key = namespaced(_CACHE_KEY)
        try:
            client.delete(key)
        except Exception as exc:
            log.warning("RenderConfigRedisMirror invalidate failed: %s", exc)
