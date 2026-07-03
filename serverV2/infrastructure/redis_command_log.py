"""Redis command observability — a transparent logging proxy around the
shared ``redis.Redis`` client.

Why: Upstash bills per command, and this codebase is deliberately tuned to
that quota (tick intervals were widened specifically to cut lock-poll
requests).  To make that cost visible, EVERY command this process sends —
direct or pipelined — is recorded with: a monotonic sequence number, a
timestamp, the command name, the target key, a derived *purpose* (what the
key is for), and the source (direct vs pipeline).  An in-memory ring holds
the recent stream; running counters expose the quota burn rate.

Design: the proxy wraps the client at the ONE choke point every caller
already uses (``RedisClient.client()``), so no call site changes.  It is
transparent — method calls and pipeline chaining behave identically; it only
observes.  Zero Redis ops are added (recording is pure in-memory), so
watching the log never inflates the very metric it measures.

Caveat: this captures commands issued by THIS server process only.  The
``backup_monitor`` cron is a separate process with its own connection and is
not reflected here.  Pipeline counting mirrors how Upstash bills a
``transaction=False`` pipeline (one request per queued command); MULTI/EXEC
wrappers on transactional pipelines are not separately counted.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

log = logging.getLogger("serverV2.redis.cmd")

_MAX_RECENT = 2000
_RATE_WINDOW_SEC = 60.0

# Map a (prefix-stripped) key to a human purpose.  First match wins.
_PURPOSE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^job:[^:]+:hb"), "worker-heartbeat"),
    (re.compile(r"^job:[^:]+:progress"), "job-progress"),
    (re.compile(r"^job:[^:]+:terminal"), "job-terminal-cache"),
    (re.compile(r"^job:[^:]+:worker_started"), "worker-start"),
    (re.compile(r"^rgmirror:"), "render-group-mirror"),
    (re.compile(r"^machines:alive"), "machine-liveness"),
    (re.compile(r"^machines:status"), "machine-status"),
    (re.compile(r"^allocation:dispatch:daemon"), "dispatch-daemon-lock"),
    (re.compile(r"^monitor:"), "daemon-lock"),
    (re.compile(r"^fleet:availability"), "fleet-snapshot"),
    (re.compile(r"^config:render"), "config-cache"),
    (re.compile(r"^modal:active"), "modal-active-jobs"),
    (re.compile(r"^pending_queue:"), "pending-queue-mirror"),
    (re.compile(r"^stats:downloads"), "download-stats"),
    (re.compile(r"^daemon:status"), "daemon-status"),
]

# Commands whose key is not the first argument.
_KEY_AT = {"eval": 2, "evalsha": 2, "fcall": 2, "fcall_ro": 2}
# Commands that carry no key (accounting/meta ops).
_NO_KEY = {"ping", "execute_command", "info", "dbsize", "flushdb", "flushall"}

_WRITE_COMMANDS = {
    "set", "setex", "hset", "hdel", "expire", "pexpire", "del", "unlink",
    "lpush", "rpush", "ltrim", "zadd", "zrem", "zremrangebyscore", "sadd",
    "srem", "incr", "incrby", "hincrby", "decr", "decrby", "getset", "mset",
    "eval", "evalsha",
}


class RedisActivity:
    """Thread-safe in-memory recorder for Redis command traffic."""

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix or ""
        self._lock = threading.Lock()
        self._recent: deque[dict] = deque(maxlen=_MAX_RECENT)
        self._events: deque[float] = deque()  # timestamps, for rolling rate
        self._total = 0
        self._by_command: dict[str, int] = defaultdict(int)
        self._by_purpose: dict[str, int] = defaultdict(int)
        self._started_at: float | None = None
        self._reset_at: float | None = None

    # -- classification ------------------------------------------------

    def _strip_prefix(self, key: str) -> str:
        if self._prefix and key.startswith(self._prefix):
            return key[len(self._prefix):]
        return key

    def _purpose_for(self, key: str) -> str:
        if not key:
            return "meta"
        stripped = self._strip_prefix(key)
        for pattern, label in _PURPOSE_RULES:
            if pattern.match(stripped):
                return label
        return "other"

    # -- recording -----------------------------------------------------

    def record(self, command: str, key: str, source: str) -> None:
        ts = time.time()
        cmd = command.lower()
        purpose = self._purpose_for(key)
        is_write = cmd in _WRITE_COMMANDS
        with self._lock:
            if self._started_at is None:
                self._started_at = ts
            self._total += 1
            seq = self._total
            self._by_command[cmd] += 1
            self._by_purpose[purpose] += 1
            self._recent.append({
                "seq": seq,
                "ts": ts,
                "command": cmd,
                "key": self._strip_prefix(key),
                "purpose": purpose,
                "source": source,
                "write": is_write,
            })
            self._events.append(ts)
            cutoff = ts - _RATE_WINDOW_SEC
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
        # DEBUG so the general /logs INFO ring isn't flooded; the dedicated
        # Redis panel is the live surface.
        log.debug("redis %s %s [%s] via=%s", cmd, key, purpose, source)

    def reset(self) -> None:
        with self._lock:
            self._recent.clear()
            self._events.clear()
            self._by_command.clear()
            self._by_purpose.clear()
            self._total = 0
            self._started_at = None
            self._reset_at = time.time()

    # -- read surface --------------------------------------------------

    def snapshot(self, recent_limit: int = 200) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            cutoff = now - _RATE_WINDOW_SEC
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            per_min = len(self._events)
            elapsed = (now - self._started_at) if self._started_at else 0.0
            recent = list(self._recent)[-recent_limit:][::-1]  # newest first
            by_command = dict(sorted(self._by_command.items(), key=lambda kv: kv[1], reverse=True))
            by_purpose = dict(sorted(self._by_purpose.items(), key=lambda kv: kv[1], reverse=True))
            total = self._total
            started_at = self._started_at
            reset_at = self._reset_at
        return {
            "total": total,
            "rate_per_min": per_min,
            "projected_per_hour": per_min * 60,
            "projected_per_day": per_min * 60 * 24,
            "projected_per_month": per_min * 60 * 24 * 30,
            "avg_per_min_since_start": round((total / (elapsed / 60.0)), 1) if elapsed > 1 else total,
            "by_command": by_command,
            "by_purpose": by_purpose,
            "started_at": started_at,
            "reset_at": reset_at,
            "recent": recent,
        }


def _extract_key(command: str, args: tuple) -> str:
    idx = _KEY_AT.get(command)
    if idx is not None:
        return str(args[idx]) if len(args) > idx else ""
    if command in _NO_KEY or not args:
        return ""
    first = args[0]
    if isinstance(first, bytes):
        try:
            return first.decode("utf-8", "replace")
        except Exception:
            return ""
    return first if isinstance(first, str) else ""


class LoggingPipeline:
    """Proxy around a redis-py pipeline that records each queued command as a
    billable request when ``execute()`` fires (matching Upstash accounting for
    non-transactional pipelines)."""

    def __init__(self, pipe, activity: RedisActivity) -> None:
        object.__setattr__(self, "_pipe", pipe)
        object.__setattr__(self, "_activity", activity)
        object.__setattr__(self, "_queued", [])

    def __getattr__(self, name: str):
        attr = getattr(self._pipe, name)
        if not callable(attr):
            return attr
        if name == "execute":
            def _execute(*a, **k):
                queued = self._queued
                for cmd, key in queued:
                    self._activity.record(cmd, key, "pipeline")
                queued.clear()
                return attr(*a, **k)
            return _execute
        if name in ("reset", "__exit__"):
            def _reset(*a, **k):
                self._queued.clear()
                return attr(*a, **k)
            return _reset
        def _wrapped(*args, **kwargs):
            self._queued.append((name, _extract_key(name, args)))
            result = attr(*args, **kwargs)
            # Pipeline command methods return the underlying pipe for chaining;
            # hand back THIS proxy so chained calls stay recorded.
            return self if result is self._pipe else result
        return _wrapped

    def __enter__(self):
        self._pipe.__enter__()
        return self

    def __exit__(self, *exc):
        self._queued.clear()
        return self._pipe.__exit__(*exc)


class LoggingRedis:
    """Transparent proxy around ``redis.Redis`` that records every command.

    Delegates all attribute access; wraps callables to record, and returns a
    ``LoggingPipeline`` from ``pipeline()``.  Non-command attributes pass
    through untouched."""

    def __init__(self, client, activity: RedisActivity) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_activity", activity)

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr
        if name == "pipeline":
            def _pipeline(*a, **k):
                return LoggingPipeline(attr(*a, **k), self._activity)
            return _pipeline
        def _wrapped(*args, **kwargs):
            self._activity.record(name, _extract_key(name, args), "direct")
            return attr(*args, **kwargs)
        return _wrapped
