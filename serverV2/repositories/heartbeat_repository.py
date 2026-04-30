"""HeartbeatRepository — ephemeral worker-liveness signal in Redis.

Two related state shapes per job:

* ``job:{id}:hb`` — a TTL'd key.  Existence is the boolean liveness
  signal: present = worker alive, missing = heartbeat expired.
* ``job:{id}:hb:window`` — a Redis LIST of the last N samples (newest
  first via LPUSH + LTRIM).  Backs the pre-render stall detector,
  which reads a sliding window of telemetry to decide if the worker
  is wedged before the first frame uploads.

Both are written in the same ``record`` call so the route only does
one round-trip from the worker's perspective.
"""

from __future__ import annotations

import json
import logging
import time

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_TTL_SEC = 60
_KEY_FMT = "job:{job_id}:hb"
_WINDOW_KEY_FMT = "job:{job_id}:hb:window"
_WINDOW_MAX_SAMPLES = 60
_WINDOW_TTL_SEC = 60 * 30


class HeartbeatRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def record(
        self,
        job_id: str,
        phase: str | None = None,
        cpu_percent: float | None = None,
        rss_bytes: int | None = None,
        bytes_progressed: int | None = None,
        total_bytes: int | None = None,
    ) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        live_payload = json.dumps({"phase": phase or "unknown"})
        sample_payload = json.dumps({
            "ts": time.time(),
            "phase": phase,
            "cpu_percent": cpu_percent,
            "rss_bytes": rss_bytes,
            "bytes_progressed": bytes_progressed,
            "total_bytes": total_bytes,
        })
        live_key = _KEY_FMT.format(job_id=job_id)
        window_key = _WINDOW_KEY_FMT.format(job_id=job_id)
        try:
            pipe = client.pipeline(transaction=False)
            pipe.set(live_key, live_payload, ex=_TTL_SEC)
            pipe.lpush(window_key, sample_payload)
            pipe.ltrim(window_key, 0, _WINDOW_MAX_SAMPLES - 1)
            pipe.expire(window_key, _WINDOW_TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("Heartbeat record failed for %s: %s", job_id, exc)

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

    def get_recent(self, job_id: str, n: int = 20) -> list[dict]:
        """Return up to ``n`` most recent heartbeat samples, newest first.
        Empty list if Redis is unavailable, the window is empty, or any
        sample is malformed (we drop the bad ones rather than poisoning
        the entire detector evaluation)."""
        client = self._redis_client.client()
        if client is None:
            return []
        n = max(1, min(n, _WINDOW_MAX_SAMPLES))
        try:
            raw = client.lrange(_WINDOW_KEY_FMT.format(job_id=job_id), 0, n - 1)
        except redis.RedisError as exc:
            log.warning("Heartbeat LRANGE failed for %s: %s", job_id, exc)
            return []
        out: list[dict] = []
        for entry in raw:
            try:
                out.append(json.loads(entry))
            except (TypeError, ValueError):
                continue
        return out

    def clear(self, job_id: str) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            pipe = client.pipeline(transaction=False)
            pipe.delete(_KEY_FMT.format(job_id=job_id))
            pipe.delete(_WINDOW_KEY_FMT.format(job_id=job_id))
            pipe.execute()
        except redis.RedisError:
            pass
