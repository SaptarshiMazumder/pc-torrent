"""Redis-backed heartbeat store.

Workers push heartbeats via HTTP — the endpoint SETs a key with a short TTL.
Pollers just check key existence: if Redis auto-expired it, the worker is dead.

No DB involvement in the heartbeat hot path.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis

log = logging.getLogger(__name__)

_HEARTBEAT_TTL_SEC = 60
_KEY_FMT = "job:{job_id}:hb"

_client: redis.Redis | None = None


def init() -> None:
    global _client
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        log.warning("REDIS_URL not set — heartbeat store disabled")
        return
    try:
        _client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        _client.ping()
        log.info("Redis heartbeat store initialized")
    except Exception as exc:
        log.error("Redis init failed (%s) — heartbeat store disabled", exc)
        _client = None


def _key(job_id: str) -> str:
    return _KEY_FMT.format(job_id=job_id)


def record(job_id: str, phase: str | None = None) -> None:
    if _client is None:
        return
    payload = json.dumps({"phase": phase or "unknown"})
    try:
        _client.set(_key(job_id), payload, ex=_HEARTBEAT_TTL_SEC)
    except redis.RedisError as exc:
        log.warning("Heartbeat SET failed for %s: %s", job_id, exc)


def is_alive(job_id: str) -> bool | None:
    """Return True if heartbeat key exists, False if missing.
    Returns None if Redis is unavailable (caller should skip the check)."""
    if _client is None:
        return None
    try:
        return _client.exists(_key(job_id)) > 0
    except redis.RedisError as exc:
        log.warning("Heartbeat EXISTS failed for %s: %s", job_id, exc)
        return None


def clear(job_id: str) -> None:
    if _client is None:
        return
    try:
        _client.delete(_key(job_id))
    except redis.RedisError:
        pass
