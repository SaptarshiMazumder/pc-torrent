"""RenderGroupRedisMirror — Redis CRUD for the active render-group DTO.

Pure I/O, zero business logic.  One HASH per user, ``rgmirror:{user_id}``,
field ``group_id`` -> the JSON active-status DTO.  Holds ONLY active groups
(terminal groups serve from Postgres row snapshots).  Only
``RenderGroupTelemetryService`` talks to this.

Fail-open: if Redis is unavailable, reads return None/{} and writes no-op,
so the caller falls back to a Postgres build.  1h TTL is a self-cleaning
safety net in case a terminal transition's remove is ever missed.

Key is un-prefixed (id-keyed): the Firebase ``user_id`` is env-safe, matching
the ``job:{id}:*`` convention in this codebase.
"""

from __future__ import annotations

import json
import logging

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_KEY_FMT = "rgmirror:{user_id}"
_TTL_SEC = 60 * 60  # 1h self-cleaning safety net


class RenderGroupRedisMirror:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    @staticmethod
    def _key(user_id: str) -> str:
        return _KEY_FMT.format(user_id=user_id)

    def write(self, user_id: str, group_id: str, dto: dict) -> None:
        if not user_id or not group_id:
            return
        client = self._redis_client.client()
        if client is None:
            return
        try:
            key = self._key(user_id)
            pipe = client.pipeline(transaction=False)
            pipe.hset(key, group_id, json.dumps(dto))
            pipe.expire(key, _TTL_SEC)
            pipe.execute()
        except (redis.RedisError, TypeError, ValueError) as exc:
            log.warning("RenderGroupRedisMirror.write(%s) failed: %s", group_id, exc)

    def remove(self, user_id: str, group_id: str) -> None:
        if not user_id or not group_id:
            return
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.hdel(self._key(user_id), group_id)
        except redis.RedisError as exc:
            log.warning("RenderGroupRedisMirror.remove(%s) failed: %s", group_id, exc)

    def read(self, user_id: str, group_id: str) -> dict | None:
        if not user_id or not group_id:
            return None
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            raw = client.hget(self._key(user_id), group_id)
        except redis.RedisError as exc:
            log.warning("RenderGroupRedisMirror.read(%s) failed: %s", group_id, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def read_active(self, user_id: str) -> dict[str, dict]:
        """Return ``{group_id: dto}`` for all of the user's mirrored active
        groups.  Empty dict on Redis error / no entries."""
        if not user_id:
            return {}
        client = self._redis_client.client()
        if client is None:
            return {}
        try:
            raw_map = client.hgetall(self._key(user_id))
        except redis.RedisError as exc:
            log.warning("RenderGroupRedisMirror.read_active(%s) failed: %s", user_id, exc)
            return {}
        out: dict[str, dict] = {}
        for gid, raw in (raw_map or {}).items():
            try:
                out[gid] = json.loads(raw)
            except (TypeError, ValueError):
                continue
        return out
