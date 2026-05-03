"""FleetAvailabilitySnapshotCache — Redis-cached snapshot with 60s TTL.

Read path:
  * ``get_or_build()`` — reads the cached value from Redis.  On miss
    (no key OR TTL expired), rebuilds via ``FleetAvailabilityBuilder``
    (parallel internally) and writes the new value with ``EX 60``.

Write path:
  * ``persist(snapshot)`` — writes the snapshot back to the same key
    with ``KEEPTTL XX`` so the freshness window is NOT extended on
    every mid-tick mutation.  If the key has expired between read and
    write (race), the ``XX`` flag makes the write a no-op — next
    ``get_or_build()`` does a full rebuild.

TTL behaviour:
  * Set once on rebuild (``EX 60``).
  * Preserved on subsequent writes (``KEEPTTL``).
  * Expires naturally → next reader rebuilds, resetting TTL.

Source-of-truth refresh cadence is therefore exactly every 60s
regardless of how many ticks fire in between.

Serialization:
  Pickle.  The snapshot carries ``FleetCapability`` and
  ``CommunityMachine`` dataclasses; pickle handles them losslessly.
  Redis is internal infra (trusted), so pickle's known security
  caveats around untrusted input do not apply.

Fail-soft:
  If Redis is unreachable on read, falls through to the in-process
  builder (rebuild-from-source) without caching.  Allocation never
  hard-fails on availability cache problems.
"""

from __future__ import annotations

import logging
import pickle

from serverV2.fleets.fleet_availability.fleet_availability_builder_factory import (
    FleetAvailabilityBuilderFactory,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)
from serverV2.infrastructure.redis_client import RedisClient

_KEY = "fleet:availability:snapshot"
_TTL_S = 60

log = logging.getLogger(__name__)


class FleetAvailabilitySnapshotCache:

    def __init__(
        self,
        *,
        builder_factory: FleetAvailabilityBuilderFactory,
        redis_client: RedisClient,
    ) -> None:
        self._builder_factory = builder_factory
        self._redis_client = redis_client

    def get_or_build(self) -> FleetAvailabilitySnapshot:
        client = self._redis_client.client()
        if client is not None:
            try:
                cached = client.get(_KEY)
            except Exception as exc:
                log.warning(
                    "FleetAvailabilitySnapshotCache: Redis GET failed (%s); "
                    "falling through to rebuild without caching",
                    exc,
                )
                cached = None
            if cached is not None:
                try:
                    return pickle.loads(cached)
                except Exception as exc:
                    log.warning(
                        "FleetAvailabilitySnapshotCache: failed to unpickle "
                        "cached snapshot (%s); rebuilding",
                        exc,
                    )

        snapshot = self._build_fresh()
        if client is not None:
            try:
                client.set(_KEY, pickle.dumps(snapshot), ex=_TTL_S)
            except Exception as exc:
                log.warning(
                    "FleetAvailabilitySnapshotCache: Redis SET on rebuild "
                    "failed (%s); next read will rebuild again",
                    exc,
                )
        return snapshot

    def persist(self, snapshot: FleetAvailabilitySnapshot) -> None:
        """Update the cached value.  Preserves the existing TTL via
        ``KEEPTTL`` and only writes if the key still exists (``XX``)
        so an expired-between-read-and-write race becomes a no-op.
        """
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.set(_KEY, pickle.dumps(snapshot), keepttl=True, xx=True)
        except Exception as exc:
            log.warning(
                "FleetAvailabilitySnapshotCache: Redis SET on persist "
                "failed (%s); cached snapshot left untouched",
                exc,
            )

    def _build_fresh(self) -> FleetAvailabilitySnapshot:
        return (
            self._builder_factory.new()
            .check_vast()
            .check_modal()
            .check_community()
            .check_in_progress_serverless_fleet()
            .build()
        )
