"""DaemonStatusRepository — per-tick heartbeat for the background singletons.

Each daemon (vast/modal/community fleet monitors + the dispatch daemon) calls
``report(...)`` at the end of every tick.  The admin dashboard reads them all
with a single ``HGETALL`` and shows a real health row per daemon: is it
ticking, who owns it, how many ticks, what it did last cycle, and any error.

Cost-aware by design (this codebase watches its Upstash quota):
  * ALL daemons share ONE hash ``daemon:status:all`` (field per daemon), so
    the dashboard reads every heartbeat in a single command.
  * Writes are throttled to at most once per ``_MIN_WRITE_INTERVAL`` per
    daemon even though daemons tick more often — enough to show "last tick
    was recent" without a write every 10s.  An error report bypasses the
    throttle so failures surface immediately.
  * A 120s TTL means a dead daemon's row goes stale then disappears.

Fail-open: a Redis blip drops the heartbeat, never breaks the daemon tick.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import redis

from serverV2.infrastructure.redis_client import RedisClient, namespaced

log = logging.getLogger(__name__)

_KEY = "daemon:status:all"
_TTL_SEC = 120
_MIN_WRITE_INTERVAL = 15.0

# The supervised singletons, in display order.
KNOWN_DAEMONS = (
    "vast_monitor",
    "modal_monitor",
    "community_monitor",
    "dispatch_daemon",
)


class DaemonStatusRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client
        self._last_write: dict[str, float] = {}
        self._lock = threading.Lock()

    def report(
        self,
        name: str,
        owner: str,
        tick_count: int,
        detail: dict | None = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            if error is None and (now - self._last_write.get(name, 0.0)) < _MIN_WRITE_INTERVAL:
                return
            self._last_write[name] = now
        client = self._redis_client.client()
        if client is None:
            return
        payload = json.dumps({
            "owner": owner,
            "tick_count": tick_count,
            "last_tick_ts": now,
            "detail": detail or {},
            "error": (error or None) if error is None else str(error)[:500],
        })
        try:
            pipe = client.pipeline(transaction=False)
            pipe.hset(namespaced(_KEY), name, payload)
            pipe.expire(namespaced(_KEY), _TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning("DaemonStatus report(%s) failed: %s", name, exc)

    def read_all(self) -> dict[str, dict]:
        """``{daemon_name: status}`` for every reporting daemon.  Empty on
        Redis error / no heartbeats."""
        client = self._redis_client.client()
        if client is None:
            return {}
        try:
            raw = client.hgetall(namespaced(_KEY))
        except redis.RedisError as exc:
            log.warning("DaemonStatus read_all failed: %s", exc)
            return {}
        out: dict[str, dict] = {}
        for name, val in (raw or {}).items():
            try:
                out[name] = json.loads(val)
            except (TypeError, ValueError):
                continue
        return out
