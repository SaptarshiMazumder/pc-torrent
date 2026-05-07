"""MachineRedisMirror — Redis-side state for community machines.

Two parallel concerns, both backed by Redis:

1. **Liveness** -- which agents are currently pinging.
   Single sorted set ``machines:alive`` keyed by machine_id, scored by
   unix timestamp of the last heartbeat.  One Redis read returns all
   alive ids regardless of pool size; no N+1 EXISTS calls.  Redis is
   the **source of truth** here -- no PG counterpart.

     Heartbeat write     : ZADD machines:alive <unix_ts> <machine_id>
     Read alive cohort   : ZRANGEBYSCORE machines:alive (now - stale) +inf
     Drop on disconnect  : ZREM machines:alive <machine_id>
     Periodic prune      : ZREMRANGEBYSCORE machines:alive 0 (now - stale)

2. **Status cache** -- mirror of ``machines.status`` from Postgres.
   Single Redis hash ``machines:status`` mapping machine_id -> status
   (``idle`` / ``available`` / ``processing``).  Postgres remains the
   source of truth on disk; Redis is the fast read path used by the
   allocator and the heartbeat-driven self-heal in
   ``JobService.next_for_machine``.

     Status write     : HSET machines:status <id> <status>
     Status read      : HGET machines:status <id>
     All ids by status: HGETALL machines:status (filter)

Renamed from ``MachineHeartbeatRepository`` -- the old name understated
the surface (status mirror was already living here).  Same Redis keys
and same wire format; only the class name and the write timing for the
status mirror have changed.
"""

from __future__ import annotations

import logging
import time

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_SET_KEY = "machines:alive"
_STATUS_KEY = "machines:status"
# 24-hour expiry on the alive set.  Individual entries get pruned by
# the CommunityMonitor's periodic prune call; this TTL is just a safety
# net so a forgotten Redis instance doesn't accumulate forever.
# The status hash uses the same TTL strategy.
_SET_TTL_SEC = 24 * 60 * 60


class MachineRedisMirror:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    # ------------------------------------------------------------------
    # liveness — Redis is source of truth, sync ops
    # ------------------------------------------------------------------

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
                "MachineRedisMirror record failed for %s: %s", machine_id, exc,
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
            log.warning("MachineRedisMirror alive_ids failed: %s", exc)
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
            log.warning("MachineRedisMirror prune_stale failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # status cache (mirror of machines.status)
    # ------------------------------------------------------------------

    def set_status(self, machine_id: str, status: str) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            pipe = client.pipeline(transaction=False)
            pipe.hset(_STATUS_KEY, machine_id, status)
            pipe.expire(_STATUS_KEY, _SET_TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning(
                "MachineRedisMirror set_status failed for %s -> %s: %s",
                machine_id, status, exc,
            )

    def clear_status(self, machine_id: str) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.hdel(_STATUS_KEY, machine_id)
        except redis.RedisError:
            pass

    def get_status(self, machine_id: str) -> str | None:
        """Read the cached status.  Returns None on cache miss OR Redis
        unavailable -- callers must fall back to Postgres."""
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            value = client.hget(_STATUS_KEY, machine_id)
        except redis.RedisError as exc:
            log.warning("MachineRedisMirror get_status failed for %s: %s", machine_id, exc)
            return None
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else value

    def available_ids(self) -> set[str] | None:
        """Set of machine ids whose cached status is ``available``.
        Returns None if Redis is unavailable so callers can fall back
        to Postgres."""
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            entries = client.hgetall(_STATUS_KEY)
        except redis.RedisError as exc:
            log.warning("MachineRedisMirror available_ids failed: %s", exc)
            return None
        out: set[str] = set()
        for mid, status in entries.items():
            mid_s = mid.decode() if isinstance(mid, bytes) else mid
            status_s = status.decode() if isinstance(status, bytes) else status
            if status_s == "available":
                out.add(mid_s)
        return out

