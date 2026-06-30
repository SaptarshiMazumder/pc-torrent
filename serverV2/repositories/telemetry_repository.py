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
        # ── Phase 11 data-richness fields (all optional, persisted when
        # available; NULL when the worker / orchestrator can't supply it).
        gpu_model_normalized: str | None = None,
        device_used: str | None = None,
        blender_version: str | None = None,
        worker_image_version: str | None = None,
        peak_vram_mb: int | None = None,
        denoiser_used: str | None = None,
        gpu_count: int | None = None,
        cpu_cores: int | None = None,
        ram_gb: float | None = None,
        startup_seconds: int | None = None,
        render_seconds: int | None = None,
        retry_count: int | None = None,
        failure_reason: str | None = None,
        worker_log_url: str | None = None,
        gpu_specs: dict[str, Any] | None = None,
    ) -> None:
        """Insert a single telemetry row.

        Predictions (``seconds_estimated`` / ``cost_estimated_usd``) are
        nullable in v1 — they stay None until Phase 6/7 wires the cost
        analyzer into the dispatch path.  Once stamped, calibration can
        compute predicted-vs-actual ratios per (fleet, gpu_type, heaviness).

        Phase 11 fields (gpu_model_normalized through gpu_specs) are
        collected for future estimation work.  They are NOT used by the
        render path -- pure observation.  Callers should swallow any
        upstream parsing errors and pass ``None`` rather than failing
        the telemetry write.
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
                heaviness_json, file_size_bytes,
                gpu_model_normalized, device_used,
                blender_version, worker_image_version,
                peak_vram_mb, denoiser_used,
                gpu_count, cpu_cores, ram_gb,
                startup_seconds, render_seconds,
                retry_count, failure_reason,
                worker_log_url, gpu_specs_json
            )
            VALUES (
                %s,%s,%s, %s,%s,%s, %s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s,
                %s,%s, %s,%s, %s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s
            )
            """,
            (
                str(uuid4()), job_id, group_id,
                fleet, gpu_type, machine_id,
                chunk_size, rendered_frames,
                started_at, completed_at, seconds_total,
                price_per_hour, cost_actual_usd,
                seconds_estimated, cost_estimated_usd,
                json.dumps(heaviness or {}), file_size_bytes,
                gpu_model_normalized, device_used,
                blender_version, worker_image_version,
                peak_vram_mb, denoiser_used,
                gpu_count, cpu_cores, ram_gb,
                startup_seconds, render_seconds,
                retry_count, failure_reason,
                worker_log_url,
                json.dumps(gpu_specs) if gpu_specs else None,
            ),
        )
