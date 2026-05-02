"""MonitorLockRepository — per-monitor-instance Redis lock.

Coordinates ownership of a monitor across N Cloud Run instances:

* ``try_acquire``  — atomic ``SET NX EX`` on the lock key.  Wins iff
                     no current owner.  Returns True on win.
* ``refresh``      — Lua-script atomic check-and-extend.  Returns True
                     iff the caller still holds the lock; False if the
                     value is gone or now belongs to another instance.
                     Caller MUST self-terminate on False.
* ``release``      — Lua-script atomic check-and-delete.  Best-effort
                     cleanup on clean monitor exit; the TTL covers
                     the case where the holder died without a chance
                     to release.

The Lua scripts are the only correct shape for refresh/release.  Plain
``EXPIRE`` or ``DEL`` would clobber another instance's lock if a Redis
hiccup let our lock expire and a different instance took over -- both
operations need to compare the value to ``instance_id`` first, and
that check + the action have to be atomic.

Lock keys live in Redis only; no Postgres mirror needed.  The DB row
(`jobs.status='running'` for per-job monitors, or the implicit "there
is community work to scan" for the singleton) is the durable state;
the lock is just "who is currently allowed to be running the watcher
for that work."
"""

from __future__ import annotations

import logging

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_LOCK_TTL_SEC = 60

# KEYS[1] = lock key, ARGV[1] = instance_id, ARGV[2] = ttl seconds.
# Returns 1 if the caller still holds the lock and the TTL was extended,
# 0 if another instance owns it (or it doesn't exist).
_REFRESH_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

# Same compare-and-act pattern for release.
_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


class MonitorLockRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def try_acquire(self, key: str, instance_id: str) -> bool:
        """Atomic ``SET NX EX``.  Returns True iff this instance now
        holds the lock for ``key``."""
        client = self._redis_client.client()
        if client is None:
            return False
        try:
            return bool(
                client.set(key, instance_id, nx=True, ex=_LOCK_TTL_SEC),
            )
        except redis.RedisError as exc:
            log.warning("MonitorLock try_acquire(%s) failed: %s", key, exc)
            return False

    def refresh(self, key: str, instance_id: str) -> bool:
        """Extend the TTL iff this instance still holds the lock.
        Returns False if another instance has taken over (or the lock
        is gone) -- caller MUST stop running the monitor."""
        client = self._redis_client.client()
        if client is None:
            # Redis blip: don't false-positive a lost-ownership.  Treat
            # as "still ours" so the monitor keeps running; the next
            # successful tick will refresh.  If Redis stays down longer
            # than _LOCK_TTL_SEC, the lock will expire and another
            # instance might take over -- a tradeoff vs killing healthy
            # monitors during a transient Redis hiccup.
            return True
        try:
            result = client.eval(
                _REFRESH_LUA, 1, key, instance_id, _LOCK_TTL_SEC,
            )
        except redis.RedisError as exc:
            log.warning("MonitorLock refresh(%s) failed: %s", key, exc)
            return True   # same blip rationale as the None branch above
        return bool(result)

    def release(self, key: str, instance_id: str) -> None:
        """Drop the lock iff we still own it.  Best-effort; failure to
        release just means the lock will expire on its own TTL."""
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.eval(_RELEASE_LUA, 1, key, instance_id)
        except redis.RedisError:
            pass

    # ------------------------------------------------------------------
    # key helpers (centralised so callers don't string-template ad-hoc)
    # ------------------------------------------------------------------

    @staticmethod
    def per_job_key(job_id: str) -> str:
        return f"monitor:job:{job_id}"

    @staticmethod
    def community_key() -> str:
        return "monitor:community"
