"""HeartbeatRepository — ephemeral worker-liveness signal in Redis.

Key existence is the only signal:
  present = worker alive, missing = heartbeat expired (worker dead).
"""

from __future__ import annotations

import json
import logging

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_TTL_SEC = 60
_KEY_FMT = "job:{job_id}:hb"


class HeartbeatRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def record(self, job_id: str, phase: str | None = None) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        payload = json.dumps({"phase": phase or "unknown"})
        try:
            client.set(_KEY_FMT.format(job_id=job_id), payload, ex=_TTL_SEC)
        except redis.RedisError as exc:
            log.warning("Heartbeat SET failed for %s: %s", job_id, exc)

    def is_alive(self, job_id: str) -> bool | None:
        """True if key exists, False if missing, None if Redis unavailable."""
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            return client.exists(_KEY_FMT.format(job_id=job_id)) > 0
        except redis.RedisError as exc:
            log.warning("Heartbeat EXISTS failed for %s: %s", job_id, exc)
            return None

    def clear(self, job_id: str) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.delete(_KEY_FMT.format(job_id=job_id))
        except redis.RedisError:
            pass
