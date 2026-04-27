"""Redis heartbeat existence check for the backup monitor."""

from __future__ import annotations

import logging

import redis

log = logging.getLogger(__name__)

HEARTBEAT_KEY_FMT = "job:{job_id}:hb"


class HeartbeatChecker:
    """Stateless check of the per-job heartbeat key in Redis.

    On Redis failure, conservatively assumes the heartbeat is alive — better
    to skip one orphan than to mass-fail jobs because Redis is hiccupping.
    """

    def is_alive(self, rds: redis.Redis, job_id: str) -> bool:
        try:
            return rds.exists(HEARTBEAT_KEY_FMT.format(job_id=job_id)) > 0
        except redis.RedisError as exc:
            log.warning("Redis EXISTS failed for job %s: %s", job_id, exc)
            return True
