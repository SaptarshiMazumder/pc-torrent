"""AntiAffinityExclusions — value object carrying the union of anti-affinity
exclusions for a single chunk's failure history.

Two parallel buckets matching ``ChunkRequest``'s wire shape:

* ``excluded_serverless_capabilities`` — pairs of ``(fleet, gpu_type)`` that
  this chunk should not be retried onto.  Modal/Vast failures land here
  because those fleets are gpu-typed; the same gpu on a different fleet is
  still eligible.
* ``excluded_machine_ids`` — community machine IDs that this chunk should
  not be retried onto.  Community failures land here because community
  machines are physical desktops, not gpu_type-keyed.

Frozen so the resolver can hand it to multiple consumers without aliasing
risk; tuples instead of sets so the field shape matches ``ChunkRequest``
verbatim and consumers don't need to convert.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AntiAffinityExclusions:
    excluded_serverless_capabilities: tuple[tuple[str, str], ...]
    excluded_machine_ids: tuple[str, ...]
