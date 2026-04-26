"""TelemetryRepository — writes one row per successful chunk to the
``render_telemetry`` table.

Used by the SuccessHandler after marking a job done.  Future calibration
tooling (Phase 5.5+) reads aggregates from this table to refine the
factor constants in ``time_analyzer.py`` and ``cost_analyzer.py``.

Phase 5 v1: write-only.  Read methods will be added when calibration
tooling actually consumes the data.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from serverV2.infrastructure.db import execute

log = logging.getLogger(__name__)


class TelemetryRepository:

    def record_chunk(
        self,
        *,
        job_id: str,
        group_id: str,
        fleet: str,
        gpu_type: str | None,
        machine_id: str | None,
        chunk_size: int,
        rendered_frames: int,
        started_at: str,
        completed_at: str,
        seconds_total: int,
        price_per_hour: float,
        cost_actual_usd: float,
        heaviness: dict[str, Any] | None = None,
        file_size_bytes: int | None = None,
        seconds_estimated: int | None = None,
        cost_estimated_usd: float | None = None,
    ) -> None:
        """Insert a single telemetry row.

        Predictions (``seconds_estimated`` / ``cost_estimated_usd``) are
        nullable in v1 — they stay None until Phase 6/7 wires the cost
        analyzer into the dispatch path.  Once stamped, calibration can
        compute predicted-vs-actual ratios per (fleet, gpu_type, heaviness).
        """
        execute(
            """
            INSERT INTO render_telemetry (
                id, job_id, group_id,
                fleet, gpu_type, machine_id,
                chunk_size, rendered_frames,
                started_at, completed_at, seconds_total,
                price_per_hour, cost_actual_usd,
                seconds_estimated, cost_estimated_usd,
                heaviness_json, file_size_bytes
            )
            VALUES (%s,%s,%s, %s,%s,%s, %s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s)
            """,
            (
                str(uuid4()), job_id, group_id,
                fleet, gpu_type, machine_id,
                chunk_size, rendered_frames,
                started_at, completed_at, seconds_total,
                price_per_hour, cost_actual_usd,
                seconds_estimated, cost_estimated_usd,
                json.dumps(heaviness or {}), file_size_bytes,
            ),
        )
