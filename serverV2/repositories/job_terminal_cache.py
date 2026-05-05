"""JobTerminalCache -- ephemeral "this job is terminal" flag in Redis.

One purpose: keep the per-heartbeat terminal-status check off the
``jobs`` Postgres table.

Heartbeats fire every ~5s per active worker.  Pre-cache, every heartbeat
did a ``SELECT * FROM jobs WHERE id = ?`` to read the current status --
~5-10ms under good conditions, much worse when the connection pool is
saturated.  This cache reduces that to a single Redis ``EXISTS`` (~1ms,
no DB round trip).

Key shape: ``job:{id}:terminal`` -- existence is the flag, no value.
TTL is generous (24h) so any worker still holding a retry-loop after
the orchestrator marks the job dead will hit the cache before its
session naturally times out.

Write path: every method on ``JobRepository`` that transitions a job
to a terminal status (``done`` / ``failed`` / ``cancelled``) calls
``mark_terminal`` after the SQL UPDATE lands.

Read path: ``JobService._assert_not_terminal`` calls ``is_terminal``;
True -> raise JobTerminalError (HTTP 410 to the worker).  False -> let
the heartbeat through.

Failure mode: if Redis is unavailable, ``is_terminal`` returns False
(fail-open).  Workers don't get the fast-exit signal but other paths
(provider monitors, Vast/Modal timeouts) still catch zombies.  Same
fallback shape that ``BackendClient.try_worker_start`` uses.
"""

from __future__ import annotations

import logging

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

_KEY_FMT = "job:{job_id}:terminal"
_TTL_SEC = 24 * 60 * 60   # 24 hours


class JobTerminalCache:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def mark_terminal(self, job_id: str) -> None:
        """Set ``job:{id}:terminal`` so the next heartbeat for this job
        bounces with 410 instead of doing a Postgres SELECT.  Safe to
        call multiple times -- idempotent SET."""
        if not job_id:
            return
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.set(_KEY_FMT.format(job_id=job_id), "1", ex=_TTL_SEC)
        except redis.RedisError as exc:
            log.warning("JobTerminalCache.mark_terminal(%s) failed: %s", job_id, exc)

    def mark_terminal_many(self, job_ids: list[str]) -> None:
        """Same as ``mark_terminal`` but pipelined -- used by
        ``cancel_active_by_group`` which transitions multiple jobs in
        one SQL UPDATE."""
        ids = [j for j in job_ids if j]
        if not ids:
            return
        client = self._redis_client.client()
        if client is None:
            return
        try:
            pipe = client.pipeline(transaction=False)
            for job_id in ids:
                pipe.set(_KEY_FMT.format(job_id=job_id), "1", ex=_TTL_SEC)
            pipe.execute()
        except redis.RedisError as exc:
            log.warning(
                "JobTerminalCache.mark_terminal_many failed for %d ids: %s",
                len(ids), exc,
            )

    def is_terminal(self, job_id: str) -> bool:
        """True if the cache flag is set.  False on miss OR if Redis is
        unavailable (fail-open -- letting heartbeats through is
        preferable to refusing them on a stale-cache fluke)."""
        if not job_id:
            return False
        client = self._redis_client.client()
        if client is None:
            return False
        try:
            return bool(client.exists(_KEY_FMT.format(job_id=job_id)))
        except redis.RedisError as exc:
            log.warning("JobTerminalCache.is_terminal(%s) failed: %s", job_id, exc)
            return False
