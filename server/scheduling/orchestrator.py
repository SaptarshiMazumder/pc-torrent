"""
Orchestrator — coordinates the render lifecycle.

Three jobs:
  1. Split frames across available machines
  2. Tell the fleet to dispatch
  3. Handle failures (retry or failover)

All implementation details (DB writes, threading, provider APIs) live in
scheduling.fleet.  This file is the WHAT, fleet.py is the HOW.
"""

from __future__ import annotations

import logging
from typing import Any

from models.render_job import RenderJob
from scheduling.fleet import PlannedTask, DispatchResult, fleet
from scheduling.frame_distribution import (
    distribute_frames,
    distribute_frames_by_chunk_size,
    expand_serverless_assignments,
)

log = logging.getLogger(__name__)


class Orchestrator:

    # ------------------------------------------------------------------
    # 1. Plan: split frames across machines
    # ------------------------------------------------------------------

    def plan(
        self,
        *,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        total_frames: int,
        machines: list[dict[str, Any]],
        scheduling: dict[str, Any],
    ) -> list[PlannedTask]:
        chunk_size = scheduling.get("chunk_size_frames")

        if chunk_size:
            raw = distribute_frames_by_chunk_size(
                frame_start=frame_start, frame_end=frame_end,
                frame_step=frame_step, machines=machines,
                chunk_size_frames=chunk_size,
            )
        else:
            raw = distribute_frames(total_frames, frame_start, frame_end, frame_step, machines)
            for i, a in enumerate(raw):
                a["chunk_index"] = i
                a["chunk_size_frames"] = None

        raw = expand_serverless_assignments(raw)
        for i, a in enumerate(raw):
            a["chunk_index"] = i

        return [
            PlannedTask(
                machine_id=a["machine_id"],
                machine_type=a.get("machine_type", "windows"),
                gpu_model=a.get("gpu_model", "Unknown"),
                gpu_vram_gb=a.get("gpu_vram_gb", 0),
                frame_start=a["frame_start"],
                frame_end=a["frame_end"],
                frame_step=a["frame_step"],
                total_frames=a["total_frames"],
                power_score=a.get("power_score", 0),
                chunk_index=a.get("chunk_index"),
                chunk_size_frames=a.get("chunk_size_frames"),
            )
            for a in raw
        ]

    # ------------------------------------------------------------------
    # 2. Execute: tell the fleet to dispatch planned tasks
    # ------------------------------------------------------------------

    def execute(
        self,
        *,
        group_id: str,
        input_filename: str,
        tasks: list[PlannedTask],
        render_overrides_json: str,
        scheduling: dict[str, Any],
    ) -> list[DispatchResult]:
        return fleet.dispatch_all(
            group_id=group_id,
            input_filename=input_filename,
            tasks=tasks,
            render_overrides_json=render_overrides_json,
            scheduling=scheduling,
        )

    # ------------------------------------------------------------------
    # 3. Handle failure: retry → failover → give up
    # ------------------------------------------------------------------

    def handle_failure(
        self,
        *,
        job: dict[str, Any] | RenderJob,
        error: str,
        group_id: str,
    ) -> str | None:
        loaded = fleet.load_job(job)
        if loaded is None:
            return None
        rj, raw_job = loaded

        remaining = rj.remaining_frames()
        if remaining is None:
            fleet.mark_done(rj.job_id)
            return None

        remaining_start, remaining_end = remaining

        # retry on same endpoint if attempts remain
        if rj.can_retry_same_endpoint():
            result = fleet.retry(rj, remaining_start, remaining_end, error, group_id)
            if result:
                return result
            error = f"Same-endpoint retry failed ({error})"

        # failover to another machine
        candidate = fleet.pick_failover(rj.machine_id, rj.machine_type)
        if candidate:
            return fleet.dispatch_failover(
                rj, raw_job, candidate,
                remaining_start, remaining_end, error, group_id,
            )

        # nothing available — mark failed
        fleet.mark_failed(rj.job_id, error)
        log.error("Job %s: no machines available for failover", rj.job_id)
        return None


orchestrator = Orchestrator()
