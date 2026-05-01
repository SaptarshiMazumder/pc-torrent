"""MachineHeartbeatRepository — community-machine liveness signal in Redis.

Parallel to ``HeartbeatRepository`` (which tracks per-job liveness).  This
class tracks per-machine liveness so the allocator can decide which
community PCs are eligible to receive new jobs.

Design: a single Redis sorted set ``machines:alive`` keyed by machine_id,
scored by unix timestamp of the last heartbeat.  Single Redis read returns
all alive ids regardless of pool size -- no N+1 EXISTS calls.

  Heartbeat write     : ZADD machines:alive <unix_ts> <machine_id>
  Read alive cohort   : ZRANGEBYSCORE machines:alive (now - stale) +inf
  Drop on disconnect  : ZREM machines:alive <machine_id>
  Periodic prune      : ZREMRANGEBYSCORE machines:alive 0 (now - stale)

Replaces the previous Postgres ``machines.last_seen_at`` write-on-every-
heartbeat pattern, which was hitting Neon connection-killed-mid-request
errors and surfacing as 500s on dependent endpoints.
"""

from __future__ import annotations

import logging
import time

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_SET_KEY = "machines:alive"
# 24-hour expiry on the set itself.  Individual entries get pruned by the
# CommunityMonitor's periodic prune call; this TTL is just a safety net so
# a forgotten Redis instance doesn't accumulate forever.
_SET_TTL_SEC = 24 * 60 * 60


class MachineHeartbeatRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def record(self, machine_id: str) -> None:
        """Stamp this machine's heartbeat with the current unix timestamp."""
        client = self._redis_client.client()
        if client is None:
            return
        try:
            pipe = client.pipeline(transaction=False)
            pipe.zadd(_SET_KEY, {machine_id: time.time()})
            pipe.expire(_SET_KEY, _SET_TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning(
                "MachineHeartbeat record failed for %s: %s", machine_id, exc,
            )

    def alive_ids(self, stale_seconds: float) -> set[str] | None:
        """Return the set of machine ids whose last heartbeat was within
        ``stale_seconds``.  Returns None if Redis is unavailable -- callers
        treat None as "fall back to whatever secondary signal exists".
        """
        client = self._redis_client.client()
        if client is None:
            return None
        cutoff = time.time() - stale_seconds
        try:
            members = client.zrangebyscore(_SET_KEY, cutoff, "+inf")
        except redis.RedisError as exc:
            log.warning("MachineHeartbeat alive_ids failed: %s", exc)
            return None
        return {m.decode() if isinstance(m, bytes) else m for m in members}

    def clear(self, machine_id: str) -> None:
        """Drop a machine from the alive set.  Called on agent
        ``set_idle`` (clean disconnect)."""
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.zrem(_SET_KEY, machine_id)
        except redis.RedisError:
            pass

    def prune_stale(self, stale_seconds: float) -> int:
        """Trim entries older than ``stale_seconds`` from the set.
        Called periodically by CommunityMonitor.  Returns the count
        removed (0 if Redis unavailable)."""
        client = self._redis_client.client()
        if client is None:
            return 0
        cutoff = time.time() - stale_seconds
        try:
            return int(client.zremrangebyscore(_SET_KEY, 0, cutoff))
        except redis.RedisError as exc:
            log.warning("MachineHeartbeat prune_stale failed: %s", exc)
            return 0
