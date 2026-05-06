"""PendingQueueRedisMirror — per-group cache of pending_allocation_queue.

Purpose
-------
Lets the detail-page poller read a single group's pending allocation rows
without hitting Postgres on every tick.  Postgres remains the source of
truth; Redis is a best-effort cache.

Key shape
---------
Per group, one HASH:

    pending_queue:group:{group_id}

Field = SQL row id (str).  Value = JSON of AllocationPendingItem.

Sentinel field ``_empty=1`` marks "cache is warm but the group has zero
pending rows", so we don't fall through to Postgres on a confirmed-empty
group.  add() drops the sentinel before writing a real row; clear_for
_group() / remove() of the last row leave the key absent (cold), which
naturally re-triggers a PG fallback the next read.

Concurrency
-----------
All writes are submitted to a small ThreadPoolExecutor and return
immediately.  The Postgres caller (AllocationPendingQueueRepository)
never blocks on Redis I/O — Redis hiccups can't extend or time out the
caller.  Reads stay synchronous because the cached result is needed to
serve the response.

All operations are fail-safe: if Redis is unreachable, writes silently
drop and reads return None (signaling cache miss → fall back to PG).
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

from serverV2.allocation.allocation_pending_queue_repository import (
    AllocationPendingItem,
)
from serverV2.infrastructure.redis_client import RedisClient

log = logging.getLogger(__name__)


_KEY_PREFIX = "pending_queue:group:"
_EMPTY_SENTINEL = "_empty"


def _key(group_id: str) -> str:
    return f"{_KEY_PREFIX}{group_id}"


def _item_to_json(item: AllocationPendingItem) -> str:
    payload: dict[str, Any] = asdict(item)
    payload["excluded_machine_ids"] = list(item.excluded_machine_ids)
    payload["excluded_serverless_capabilities"] = [
        list(p) for p in item.excluded_serverless_capabilities
    ]
    payload["machine_ids"] = list(item.machine_ids)
    return json.dumps(payload)


def _item_from_json(raw: str) -> AllocationPendingItem:
    d = json.loads(raw)
    return AllocationPendingItem(
        id=d.get("id"),
        type=d["type"],
        group_id=d["group_id"],
        chunk_index=d.get("chunk_index"),
        attempt=d.get("attempt"),
        frame_start=d["frame_start"],
        frame_end=d["frame_end"],
        frame_step=d["frame_step"],
        total_frames=d["total_frames"],
        engine=d.get("engine"),
        tier=d.get("tier"),
        excluded_machine_ids=tuple(d.get("excluded_machine_ids") or []),
        excluded_serverless_capabilities=tuple(
            tuple(p) for p in (d.get("excluded_serverless_capabilities") or [])
        ),
        render_overrides_json=d.get("render_overrides_json") or "",
        max_retries=int(d.get("max_retries") or 0),
        priority=int(d.get("priority") or 0),
        machine_ids=tuple(d.get("machine_ids") or []),
        input_filename=d.get("input_filename") or "",
        created_at=d.get("created_at") or "",
        last_attempted_at=d.get("last_attempted_at"),
    )


class PendingQueueRedisMirror:

    def __init__(
        self,
        redis_client: RedisClient,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._redis = redis_client
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="pending-queue-mirror",
        )

    # ------------------------------------------------------------------
    # writes -- fire-and-forget
    # ------------------------------------------------------------------

    def add(self, item: AllocationPendingItem) -> None:
        self._executor.submit(self._add_sync, item)

    def update_last_attempted_at(
        self, group_id: str, row_id: int, last_attempted_at: str,
    ) -> None:
        self._executor.submit(
            self._update_last_attempted_at_sync,
            group_id, row_id, last_attempted_at,
        )

    def remove(self, group_id: str, row_id: int) -> None:
        self._executor.submit(self._remove_sync, group_id, row_id)

    def clear_for_group(self, group_id: str) -> None:
        self._executor.submit(self._clear_for_group_sync, group_id)

    def set_for_group(
        self, group_id: str, items: list[AllocationPendingItem],
    ) -> None:
        self._executor.submit(self._set_for_group_sync, group_id, items)

    # ------------------------------------------------------------------
    # reads -- synchronous
    # ------------------------------------------------------------------

    def get_for_group(self, group_id: str) -> list[AllocationPendingItem] | None:
        """Return cached items, an empty list if the group is known to
        have zero rows, or ``None`` if the cache is cold for this group.
        """
        client = self._redis.client()
        if client is None:
            return None
        try:
            raw = client.hgetall(_key(group_id))
        except Exception as exc:
            log.warning("Redis HGETALL failed for %s: %s", group_id, exc)
            return None
        if not raw:
            return None
        if _EMPTY_SENTINEL in raw:
            return []
        return [_item_from_json(v) for v in raw.values()]

    # ------------------------------------------------------------------
    # internal -- run on the executor
    # ------------------------------------------------------------------

    def _add_sync(self, item: AllocationPendingItem) -> None:
        client = self._redis.client()
        if client is None or item.id is None:
            return
        try:
            key = _key(item.group_id)
            # Drop empty sentinel before writing a real row.
            client.hdel(key, _EMPTY_SENTINEL)
            client.hset(key, str(item.id), _item_to_json(item))
        except Exception as exc:
            log.warning("Redis mirror add failed for row %s: %s", item.id, exc)

    def _update_last_attempted_at_sync(
        self, group_id: str, row_id: int, last_attempted_at: str,
    ) -> None:
        client = self._redis.client()
        if client is None:
            return
        try:
            key = _key(group_id)
            current = client.hget(key, str(row_id))
            if current is None:
                return
            payload = json.loads(current)
            payload["last_attempted_at"] = last_attempted_at
            client.hset(key, str(row_id), json.dumps(payload))
        except Exception as exc:
            log.warning(
                "Redis mirror update_last_attempted_at failed for row %s: %s",
                row_id, exc,
            )

    def _remove_sync(self, group_id: str, row_id: int) -> None:
        client = self._redis.client()
        if client is None:
            return
        try:
            client.hdel(_key(group_id), str(row_id))
        except Exception as exc:
            log.warning(
                "Redis mirror remove failed for row %s: %s", row_id, exc,
            )

    def _clear_for_group_sync(self, group_id: str) -> None:
        client = self._redis.client()
        if client is None:
            return
        try:
            client.delete(_key(group_id))
        except Exception as exc:
            log.warning(
                "Redis mirror clear_for_group failed for %s: %s",
                group_id, exc,
            )

    def _set_for_group_sync(
        self, group_id: str, items: list[AllocationPendingItem],
    ) -> None:
        client = self._redis.client()
        if client is None:
            return
        try:
            key = _key(group_id)
            client.delete(key)
            if not items:
                client.hset(key, _EMPTY_SENTINEL, "1")
                return
            mapping = {
                str(item.id): _item_to_json(item)
                for item in items if item.id is not None
            }
            if mapping:
                client.hset(key, mapping=mapping)
            else:
                client.hset(key, _EMPTY_SENTINEL, "1")
        except Exception as exc:
            log.warning(
                "Redis mirror set_for_group failed for %s: %s",
                group_id, exc,
            )
