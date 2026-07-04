"""JobsLoggerRedisMirror -- pure Redis CRUD for per-worker log chunks.

Zero business logic.  One LIST per (env, group, job) holds the ordered
gzipped chunks the worker POSTs.  Only ``JobsLoggerService`` talks to
this class.

Key:
    logs:{env}:{group_id}:{job_id}
        LIST of JSON strings: ``{"offset": int, "gz": base64}``

TTL is refreshed to 10 min on every append.  Staleness ("worker went
silent") is derived directly from the key's remaining TTL -- no
separate meta hash is written, so nothing can go out of sync with the
chunks themselves.

Fail loud: this class has NO defensive try/except around Redis errors.
Any RedisError propagates up to the caller (router -> HTTP 500 -> the
worker's POST or the backup_monitor's write-logs-to-r2 tick sees the
failure immediately).  A silent swallow at this layer would exactly
reproduce the R2-tagging-bug incident where a bad SDK call was hidden
for hours across multiple render sessions.

The RedisClient in this project uses ``decode_responses=True`` -- all
values come back as ``str``.  Binary log data is therefore base64-
encoded inside the JSON envelope.  Costs ~33 % on-Redis storage
overhead vs raw bytes; acceptable given the 10 min TTL.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Iterable

from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)

TTL_SEC = 600
_KEY_LOGS_PREFIX = "logs"


class JobsLoggerRedisMirror:

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    @staticmethod
    def _key(env: str, group_id: str, job_id: str) -> str:
        return f"{_KEY_LOGS_PREFIX}:{env}:{group_id}:{job_id}"

    def _client(self):
        client = self._redis_client.client()
        if client is None:
            raise RuntimeError(
                "JobsLoggerRedisMirror: RedisClient not connected "
                "(check REDIS_URL + main.py's redis_client.connect())"
            )
        return client

    def append_chunk(
        self,
        env: str,
        group_id: str,
        job_id: str,
        offset: int,
        gzipped: bytes,
    ) -> None:
        payload = json.dumps({
            "offset": offset,
            "gz": base64.b64encode(gzipped).decode("ascii"),
        })
        key = self._key(env, group_id, job_id)
        pipe = self._client().pipeline(transaction=False)
        pipe.rpush(key, payload)
        pipe.expire(key, TTL_SEC)
        pipe.execute()

    def read_chunks(
        self, env: str, group_id: str, job_id: str,
    ) -> list[tuple[int, bytes]]:
        raw = self._client().lrange(self._key(env, group_id, job_id), 0, -1)
        out: list[tuple[int, bytes]] = []
        for item in raw or []:
            decoded = json.loads(item)
            offset = int(decoded["offset"])
            gz = base64.b64decode(decoded["gz"])
            out.append((offset, gz))
        return out

    def scan_log_keys(self) -> Iterable[tuple[str, str, str]]:
        """Yield ``(env, group_id, job_id)`` for every log key in Redis.
        Wraps SCAN, not KEYS -- safe on large keyspaces."""
        for key in self._client().scan_iter(match=f"{_KEY_LOGS_PREFIX}:*"):
            parts = key.split(":")
            if len(parts) != 4:
                raise RuntimeError(
                    f"JobsLoggerRedisMirror: scan yielded malformed key "
                    f"{key!r} -- schema drift or a foreign process is "
                    f"writing under the logs:* prefix"
                )
            _, env, group_id, job_id = parts
            yield env, group_id, job_id

    def delete(self, env: str, group_id: str, job_id: str) -> None:
        self._client().delete(self._key(env, group_id, job_id))
