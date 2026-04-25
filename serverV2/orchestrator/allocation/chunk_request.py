"""ChunkRequest — a pending chunk of work that needs a machine assigned.

Carries everything the allocator needs to pick a machine for one chunk
(used by the retry path and any future single-chunk allocation).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChunkRequest:
    group_id: str
    chunk_index: int
    frame_start: int
    frame_end: int
    frame_step: int
    total_frames: int
    attempt: int
    # Anti-affinity on retry.  Strategies must filter these out:
    #   * ``excluded_machine_ids``           — community fleet
    #   * ``excluded_serverless_capabilities`` — pairs of (fleet, gpu_type)
    excluded_machine_ids: tuple[str, ...] = field(default_factory=tuple)
    excluded_serverless_capabilities: tuple[tuple[str, str], ...] = field(
        default_factory=tuple
    )
    # Heaviness signal for FastRender-style strategies on retry.  ``None``
    # means heaviness is unknown (legacy rows pre-Phase-3).
    file_size_bytes: int | None = None
    # Render engine ("BLENDER_EEVEE", "CYCLES", ...) — fed into the validator
    # context so EngineCompatibilityValidator can keep retries off fleets
    # that can't run the engine.
    engine: str | None = None
    # Extensibility for future allocation rules (tier, price caps, ...).
    user_id: str | None = None
