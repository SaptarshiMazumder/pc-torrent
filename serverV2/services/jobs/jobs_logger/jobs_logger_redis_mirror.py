"""JobsLoggerRedisMirror -- pure Redis CRUD for per-worker log chunks.

Zero business logic.  One LIST per (env, group, job) for the ordered
chunks and one HASH for the accompanying meta.  Only
``JobsLoggerService`` talks to this class.

Keys:
    logs:{env}:{group_id}:{job_id}
        LIST of JSON strings: ``{"offset": int, "gz": base64}``
    logs:meta:{env}:{group_id}:{job_id}
        HASH: attempt, chunk_index, fleet, machine_id,
        first_seen_at, last_seen_at

Both keys carry a 10 min TTL that refreshes on every append -- an
active render keeps its keys alive; a dead worker's keys age out and
either the R2-writer catches them first, or Redis reaps them.

Fail-open: any RedisError is logged at warning and swallowed so the
worker POST path never propagates a 5xx just because Redis is having
a moment.  Reads return None/[] on failure.

The RedisClient in this project uses ``decode_responses=True`` -- all
values come back as ``str``.  Binary log data is therefore base64-
encoded inside the JSON envelope.  Costs ~33 % on-Redis storage
overhead vs raw bytes; acceptable given the 10 min TTL.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Iterable

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_TTL_SEC = 600
_KEY_LOGS_PREFIX = "logs"
_KEY_META_PREFIX = "logs:meta"


class JobsLoggerRedisMirror:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    @staticmethod
    def _key(env: str, group_id: str, job_id: str) -> str:
        return f"{_KEY_LOGS_PREFIX}:{env}:{group_id}:{job_id}"

    @staticmethod
    def _meta_key(env: str, group_id: str, job_id: str) -> str:
        return f"{_KEY_META_PREFIX}:{env}:{group_id}:{job_id}"

    def append_chunk(
        self,
        env: str,
        group_id: str,
        job_id: str,
        offset: int,
        gzipped: bytes,
    ) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            payload = json.dumps({
                "offset": offset,
                "gz": base64.b64encode(gzipped).decode("ascii"),
            })
            key = self._key(env, group_id, job_id)
            pipe = client.pipeline(transaction=False)
            pipe.rpush(key, payload)
            pipe.expire(key, _TTL_SEC)
            pipe.execute()
        except (redis.RedisError, TypeError, ValueError) as exc:
            log.warning(
                "append_chunk failed (%s:%s:%s): %s",
                env, group_id, job_id, exc,
            )

    def write_meta(
        self,
        env: str,
        group_id: str,
        job_id: str,
        first_ctx: dict,
    ) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            now = str(int(time.time()))
            key = self._meta_key(env, group_id, job_id)
            pipe = client.pipeline(transaction=False)
            pipe.hset(
                key,
                mapping={
                    "attempt":       str(first_ctx.get("attempt", "")),
                    "chunk_index":   str(first_ctx.get("chunk_index", "")),
                    "fleet":         str(first_ctx.get("fleet", "")),
                    "machine_id":    str(first_ctx.get("machine_id", "")),
                    "first_seen_at": now,
                    "last_seen_at":  now,
                },
            )
            pipe.expire(key, _TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("write_meta failed (%s): %s", job_id, exc)

    def touch_meta(self, env: str, group_id: str, job_id: str) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            now = str(int(time.time()))
            key = self._meta_key(env, group_id, job_id)
            pipe = client.pipeline(transaction=False)
            pipe.hset(key, "last_seen_at", now)
            pipe.expire(key, _TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("touch_meta failed (%s): %s", job_id, exc)

    def read_chunks(
        self, env: str, group_id: str, job_id: str,
    ) -> list[tuple[int, bytes]]:
        client = self._redis_client.client()
        if client is None:
            return []
        try:
            raw = client.lrange(self._key(env, group_id, job_id), 0, -1)
        except redis.RedisError as exc:
            log.warning("read_chunks failed (%s): %s", job_id, exc)
            return []
        out: list[tuple[int, bytes]] = []
        for item in raw or []:
            try:
                decoded = json.loads(item)
                offset = int(decoded["offset"])
                gz = base64.b64decode(decoded["gz"])
                out.append((offset, gz))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def read_meta(
        self, env: str, group_id: str, job_id: str,
    ) -> dict | None:
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            raw = client.hgetall(self._meta_key(env, group_id, job_id))
        except redis.RedisError as exc:
            log.warning("read_meta failed (%s): %s", job_id, exc)
            return None
        return dict(raw) if raw else None

    def scan_log_keys(self) -> Iterable[tuple[str, str, str]]:
        """Yield ``(env, group_id, job_id)`` for every non-meta log key
        in Redis.  Wraps SCAN, not KEYS -- safe on large keyspaces."""
        client = self._redis_client.client()
        if client is None:
            return
        try:
            for key in client.scan_iter(match=f"{_KEY_LOGS_PREFIX}:*"):
                if key.startswith(f"{_KEY_META_PREFIX}:"):
                    continue
                parts = key.split(":")
                if len(parts) != 4:
                    continue
                _, env, group_id, job_id = parts
                yield env, group_id, job_id
        except redis.RedisError as exc:
            log.warning("scan_log_keys failed: %s", exc)

    def delete(self, env: str, group_id: str, job_id: str) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            pipe = client.pipeline(transaction=False)
            pipe.delete(self._key(env, group_id, job_id))
            pipe.delete(self._meta_key(env, group_id, job_id))
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("delete failed (%s): %s", job_id, exc)
