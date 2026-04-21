"""ProgressRepository — latest render progress cached in Redis.

Workers push ``rendered_frames`` / ``total_frames`` here on each frame.
Consumers (UI status endpoint, fleet pollers) read the freshest value
without touching Postgres.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_TTL_SEC = 24 * 3600
_KEY_FMT = "job:{job_id}:progress"


@dataclass(frozen=True)
class Progress:
    rendered_frames: int
    total_frames: int


class ProgressRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def record(self, job_id: str, rendered_frames: int, total_frames: int) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        payload = json.dumps({
            "rendered_frames": int(rendered_frames or 0),
            "total_frames": int(total_frames or 0),
        })
        try:
            client.set(_KEY_FMT.format(job_id=job_id), payload, ex=_TTL_SEC)
        except redis.RedisError as exc:
            log.warning("Progress SET failed for %s: %s", job_id, exc)

    def get(self, job_id: str) -> Progress | None:
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            raw = client.get(_KEY_FMT.format(job_id=job_id))
            if not raw:
                return None
            data = json.loads(raw)
            return Progress(
                rendered_frames=int(data.get("rendered_frames", 0)),
                total_frames=int(data.get("total_frames", 0)),
            )
        except (redis.RedisError, json.JSONDecodeError, ValueError) as exc:
            log.warning("Progress GET failed for %s: %s", job_id, exc)
            return None
