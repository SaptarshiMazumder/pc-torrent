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
  JSON.  The snapshot carries only primitive-field dataclasses
  (``FleetCapability``, ``CommunityMachine``) plus a ``dict[str,int]``,
  so JSON round-trips losslessly via ``asdict`` + dataclass
  reconstruction.  Chosen over pickle because the project's shared
  Redis client uses ``decode_responses=True`` (required by the SADD /
  SCARD / ZADD work elsewhere) which can't read raw pickle bytes
  back — UTF-8 decode chokes on the 0x80 protocol marker.  JSON sails
  through, and is human-readable in Redis MONITOR for debugging.

Fail-soft:
  If Redis is unreachable on read, falls through to the in-process
  builder (rebuild-from-source) without caching.  Allocation never
  hard-fails on availability cache problems.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from serverV2.core.models import CommunityMachine, FleetCapability
from serverV2.fleets.fleet_availability.fleet_availability_builder_factory import (
    FleetAvailabilityBuilderFactory,
)
from serverV2.fleets.fleet_availability.fleet_availability_snapshot import (
    FleetAvailabilitySnapshot,
)
from serverV2.infrastructure.redis_client import RedisClient, namespaced

_KEY = namespaced("fleet:availability:snapshot")
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
                    return _deserialize(cached)
                except Exception as exc:
                    log.warning(
                        "FleetAvailabilitySnapshotCache: failed to decode "
                        "cached snapshot (%s); rebuilding",
                        exc,
                    )

        snapshot = self._build_fresh()
        if client is not None:
            try:
                client.set(_KEY, _serialize(snapshot), ex=_TTL_S)
            except Exception as exc:
                log.warning(
                    "FleetAvailabilitySnapshotCache: Redis SET on rebuild "
                    "failed (%s); next read will rebuild again",
                    exc,
                )
        return snapshot

    def peek(self) -> FleetAvailabilitySnapshot | None:
        """Cache-only read: return the cached snapshot or None on miss /
        Redis down.  NEVER rebuilds — a rebuild fans out to the Vast API,
        which a dashboard poll must not trigger."""
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            cached = client.get(_KEY)
        except Exception as exc:
            log.warning("FleetAvailabilitySnapshotCache: peek GET failed (%s)", exc)
            return None
        if cached is None:
            return None
        try:
            return _deserialize(cached)
        except Exception as exc:
            log.warning(
                "FleetAvailabilitySnapshotCache: peek decode failed (%s)", exc,
            )
            return None

    def persist(self, snapshot: FleetAvailabilitySnapshot) -> None:
        """Update the cached value.  Preserves the existing TTL via
        ``KEEPTTL`` and only writes if the key still exists (``XX``)
        so an expired-between-read-and-write race becomes a no-op.
        """
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.set(_KEY, _serialize(snapshot), keepttl=True, xx=True)
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


# ----------------------------------------------------------------------
# JSON codec — module-private helpers, kept here because they belong
# next to the cache that owns the wire format.
# ----------------------------------------------------------------------

def _serialize(snapshot: FleetAvailabilitySnapshot) -> str:
    return json.dumps({
        "vast_available":       [asdict(c) for c in snapshot.vast_available],
        "modal_available":      [asdict(c) for c in snapshot.modal_available],
        "community_available":  [asdict(m) for m in snapshot.community_available],
        "serverless_in_flight": dict(snapshot.serverless_in_flight),
    })


def _deserialize(raw: str) -> FleetAvailabilitySnapshot:
    data = json.loads(raw)
    return FleetAvailabilitySnapshot(
        vast_available=tuple(FleetCapability(**c) for c in data["vast_available"]),
        modal_available=tuple(FleetCapability(**c) for c in data["modal_available"]),
        community_available=tuple(
            CommunityMachine(**m) for m in data["community_available"]
        ),
        serverless_in_flight=dict(data["serverless_in_flight"]),
    )
