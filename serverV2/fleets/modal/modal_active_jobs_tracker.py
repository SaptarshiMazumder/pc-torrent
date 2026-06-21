"""ModalActiveJobsTracker — Redis-backed live count of active Modal jobs.

Source of truth for the per-GPU and fleet-wide caps in
``ModalAvailabilityBuilder``.  PG (``JobRepository.count_active_by_
fleet_and_gpu_type``) is the fallback when Redis is unreachable.

Schema:

    Key:    "modal:active:{gpu_type}"     # e.g. "modal:active:l4"
    Value:  Redis SET of job_id strings
    TTL:    86400 seconds (1 day) -- safety net; refreshed on every
            ``add()`` to that key so as long as the fleet has live
            traffic the keys never actually expire.

Idempotent: SADD on an existing member is a no-op, SREM on a missing
member is a no-op.  Race-condition double-fires from multiple cleanup
paths (callback handler + lifecycle hook) don't break the count.

Fail-soft on Redis unavailability:
* ``add`` / ``remove`` log and continue if Redis is down.  PG remains
  authoritative -- on the next read fallback the count will be correct.
* ``count_for_gpu_types`` returns ``None`` when Redis is unreachable
  so the caller falls back to PG explicitly (rather than silently
  returning zeros that would unblock dispatch incorrectly).
"""

from __future__ import annotations

import logging

from serverV2.infrastructure.redis_client import RedisClient, namespaced


_KEY_PREFIX = namespaced("modal:active")
_TTL_SECONDS = 86400  # 1 day

log = logging.getLogger(__name__)


class ModalActiveJobsTracker:

    def __init__(self, *, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    def add(self, job_id: str, gpu_type: str) -> None:
        """Mark a Modal job as active.  Called once per dispatch."""
        client = self._redis_client.client()
        if client is None:
            log.warning(
                "ModalActiveJobsTracker.add: Redis unavailable; skipping "
                "(job_id=%s gpu_type=%s)", job_id, gpu_type,
            )
            return
        key = self._key(gpu_type)
        try:
            client.sadd(key, job_id)
            client.expire(key, _TTL_SECONDS)
        except Exception as exc:
            log.warning(
                "ModalActiveJobsTracker.add: Redis error (%s); skipping "
                "(job_id=%s gpu_type=%s)", exc, job_id, gpu_type,
            )

    def remove(self, job_id: str, gpu_type: str) -> None:
        """Drop a Modal job from the active set.  Called once per
        terminal transition.  Idempotent."""
        client = self._redis_client.client()
        if client is None:
            log.warning(
                "ModalActiveJobsTracker.remove: Redis unavailable; skipping "
                "(job_id=%s gpu_type=%s)", job_id, gpu_type,
            )
            return
        try:
            client.srem(self._key(gpu_type), job_id)
        except Exception as exc:
            log.warning(
                "ModalActiveJobsTracker.remove: Redis error (%s); skipping "
                "(job_id=%s gpu_type=%s)", exc, job_id, gpu_type,
            )

    def count_for_gpu_types(
        self, gpu_types: list[str],
    ) -> dict[str, int] | None:
        """Returns ``{gpu_type: in_flight_count}`` for each requested
        type.  Returns ``None`` if Redis is unreachable -- caller falls
        back to ``JobRepository.count_active_by_fleet_and_gpu_type``."""
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            return {gt: int(client.scard(self._key(gt))) for gt in gpu_types}
        except Exception as exc:
            log.warning(
                "ModalActiveJobsTracker.count_for_gpu_types: Redis error "
                "(%s); falling back to PG", exc,
            )
            return None

    @staticmethod
    def _key(gpu_type: str) -> str:
        return f"{_KEY_PREFIX}:{gpu_type}"
