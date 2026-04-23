"""WorkerStartRepository — records which job_ids a worker has already begun.

Defeats Modal's silent re-queue: when a Modal container dies mid-execution,
Modal can re-invoke our function with the same input.  The worker calls
:meth:`try_claim` as its very first action; the first caller wins, any
later caller for the same ``job_id`` gets ``False`` and must abort.

Backed by Redis ``SET NX EX`` for atomicity across Cloud Run instances.
"""

from __future__ import annotations

import logging

import redis

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

# 25 hours — Modal's function timeout is 24h, so the claim outlives the
# longest possible legitimate render.  Orchestrator retries use new job_ids,
# so a stuck claim never blocks legitimate retries.
_TTL_SEC = 25 * 60 * 60
_KEY_FMT = "job:{job_id}:worker_started"


class WorkerStartRepository:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def try_claim(self, job_id: str) -> bool:
        """Return True if this caller is the first to start the worker for
        ``job_id``.  Return False if another caller already claimed it —
        the caller must abort without doing any work.

        Fail-open: if Redis is unavailable, return True so we don't break
        the happy path.  A Redis outage disables the duplicate-start guard
        but does not prevent renders.
        """
        client = self._redis_client.client()
        if client is None:
            log.warning("WorkerStart Redis unavailable — failing open for %s", job_id)
            return True
        try:
            acquired = client.set(
                _KEY_FMT.format(job_id=job_id),
                "1",
                nx=True,
                ex=_TTL_SEC,
            )
        except redis.RedisError as exc:
            log.warning("WorkerStart SET failed for %s: %s", job_id, exc)
            return True
        return bool(acquired)
