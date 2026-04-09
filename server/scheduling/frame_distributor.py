"""
Frame distributor — pure scheduling policy, no I/O.

Responsibilities:
- Score machines by GPU power
- Cap worker count to match the frame budget
- Distribute frame ranges across machines (proportional or fixed-chunk)
- Expand serverless assignments into parallel sub-jobs

These functions take plain dicts and return plain dicts so they are
fully testable without a database or network.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from domain.value_objects import MIN_FRAMES_PER_WORKER, SERVERLESS_TYPES, MACHINE_STALE_SECONDS

try:
    WORKERS_PER_SERVERLESS = max(1, int(os.getenv("WORKERS_PER_SERVERLESS", "3")))
except ValueError:
    WORKERS_PER_SERVERLESS = 3


# ---------------------------------------------------------------------------
# Machine scoring
# ---------------------------------------------------------------------------

def compute_power_score(machine: dict[str, Any]) -> float:
    """GPU-weighted rendering power score."""
    vram = machine.get("gpu_vram_gb") or 0
    cores = machine.get("cpu_cores") or 0
    ram = machine.get("ram_gb") or 0
    return (vram * 4) + (cores * 1) + (ram * 0.3)


def filter_enabled_machines(machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop machines whose provision strategy is currently disabled."""
    from scheduling.strategies import get_strategy

    enabled: list[dict[str, Any]] = []
    for machine in machines:
        machine_type = machine.get("machine_type", "windows")
        if get_strategy(machine_type).is_enabled():
            enabled.append(machine)
    return enabled


# ---------------------------------------------------------------------------
# Worker budget capping
# ---------------------------------------------------------------------------

def max_workers_for_frame_budget(total_frames: int, requested_workers: int) -> int:
    """Cap workers so each gets at least MIN_FRAMES_PER_WORKER frames."""
    if requested_workers <= 1:
        return 1
    if total_frames <= 0:
        return 1
    return max(1, min(requested_workers, total_frames // MIN_FRAMES_PER_WORKER))


def limit_machines_for_frame_budget(
    machines: list[dict[str, Any]],
    total_frames: int,
) -> list[dict[str, Any]]:
    """Keep only as many machines as the frame budget can justify."""
    if not machines:
        return []
    allowed = max_workers_for_frame_budget(total_frames, len(machines))
    if allowed >= len(machines):
        return machines
    ranked = sorted(machines, key=compute_power_score, reverse=True)
    return ranked[:allowed]


# ---------------------------------------------------------------------------
# Frame distribution strategies
# ---------------------------------------------------------------------------

def distribute_frames(
    total_frames: int,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    machines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Split frames across machines proportionally to their power scores."""
    machines = limit_machines_for_frame_budget(machines, total_frames)
    if not machines:
        return []

    scores = [(m, compute_power_score(m)) for m in machines]
    total_score = sum(s for _, s in scores)
    if total_score <= 0:
        total_score = len(machines)
        scores = [(m, 1.0) for m in machines]

    assignments: list[dict[str, Any]] = []
    current_frame = frame_start

    for i, (machine, score) in enumerate(scores):
        if i == len(scores) - 1:
            chunk_end = frame_end
        else:
            share = score / total_score
            chunk_frames = max(1, round(total_frames * share))
            chunk_end = min(current_frame + (chunk_frames - 1) * frame_step, frame_end)

        chunk_total = (
            ((chunk_end - current_frame) // frame_step) + 1
            if chunk_end >= current_frame
            else 0
        )
        assignments.append({
            "machine_id": machine["id"],
            "machine_type": machine.get("machine_type", "windows"),
            "gpu_model": machine.get("gpu_model", "Unknown"),
            "gpu_vram_gb": machine.get("gpu_vram_gb", 0),
            "cpu_cores": machine.get("cpu_cores", 0),
            "ram_gb": machine.get("ram_gb", 0),
            "frame_start": current_frame,
            "frame_end": chunk_end,
            "frame_step": frame_step,
            "total_frames": chunk_total,
            "power_score": round(score, 1),
        })
        current_frame = chunk_end + frame_step

    return assignments


def distribute_frames_by_chunk_size(
    frame_start: int,
    frame_end: int,
    frame_step: int,
    machines: list[dict[str, Any]],
    chunk_size_frames: int,
) -> list[dict[str, Any]]:
    """Split frame range into fixed-size chunks assigned in power-ranked round-robin."""
    if chunk_size_frames < 1:
        raise ValueError("chunk_size_frames must be >= 1")
    effective_chunk_size = max(chunk_size_frames, MIN_FRAMES_PER_WORKER)
    total_frames = (
        ((frame_end - frame_start) // frame_step) + 1 if frame_end >= frame_start else 0
    )
    machines = limit_machines_for_frame_budget(machines, total_frames)
    if not machines:
        return []

    ranked = sorted(
        [(m, compute_power_score(m)) for m in machines],
        key=lambda item: item[1],
        reverse=True,
    )

    assignments: list[dict[str, Any]] = []
    current_frame = frame_start
    chunk_index = 0

    while current_frame <= frame_end:
        machine, score = ranked[chunk_index % len(ranked)]
        frames_remaining = ((frame_end - current_frame) // frame_step) + 1

        if frames_remaining <= effective_chunk_size:
            frames_for_chunk = frames_remaining
        else:
            frames_for_chunk = effective_chunk_size
            tail_frames = frames_remaining - frames_for_chunk
            if 0 < tail_frames < MIN_FRAMES_PER_WORKER:
                frames_for_chunk = frames_remaining

        chunk_end = min(current_frame + (frames_for_chunk - 1) * frame_step, frame_end)
        chunk_total = (
            ((chunk_end - current_frame) // frame_step) + 1
            if chunk_end >= current_frame
            else 0
        )

        assignments.append({
            "machine_id": machine["id"],
            "machine_type": machine.get("machine_type", "windows"),
            "gpu_model": machine.get("gpu_model", "Unknown"),
            "gpu_vram_gb": machine.get("gpu_vram_gb", 0),
            "cpu_cores": machine.get("cpu_cores", 0),
            "ram_gb": machine.get("ram_gb", 0),
            "frame_start": current_frame,
            "frame_end": chunk_end,
            "frame_step": frame_step,
            "total_frames": chunk_total,
            "power_score": round(score, 1),
            "chunk_index": chunk_index,
            "chunk_size_frames": effective_chunk_size,
        })
        current_frame = chunk_end + frame_step
        chunk_index += 1

    return assignments


# ---------------------------------------------------------------------------
# Serverless parallelism expansion
# ---------------------------------------------------------------------------

def expand_serverless_assignments(
    assignments: list[dict[str, Any]],
    workers_per_endpoint: int = WORKERS_PER_SERVERLESS,
) -> list[dict[str, Any]]:
    """Split each serverless assignment into parallel sub-assignments."""
    from scheduling.strategies import get_strategy

    expanded: list[dict[str, Any]] = []
    for a in assignments:
        mt = a.get("machine_type", "")
        if mt not in SERVERLESS_TYPES:
            expanded.append(a)
            continue

        effective_workers = get_strategy(mt).workers_per_endpoint
        if effective_workers <= 1:
            expanded.append(a)
            continue

        total_frames = a["total_frames"]
        if total_frames <= 1:
            expanded.append(a)
            continue

        worker_count = max_workers_for_frame_budget(total_frames, effective_workers)
        if worker_count <= 1:
            expanded.append(a)
            continue

        frame_start = a["frame_start"]
        frame_end = a["frame_end"]
        frame_step = a["frame_step"]
        frames_per_worker = max(1, total_frames // worker_count)
        current = frame_start

        for w in range(worker_count):
            if current > frame_end:
                break
            sub_end = (
                frame_end
                if w == worker_count - 1
                else min(current + (frames_per_worker - 1) * frame_step, frame_end)
            )
            sub_total = (
                ((sub_end - current) // frame_step) + 1 if sub_end >= current else 0
            )
            if sub_total <= 0:
                break

            sub = dict(a)
            sub["frame_start"] = current
            sub["frame_end"] = sub_end
            sub["total_frames"] = sub_total
            expanded.append(sub)
            current = sub_end + frame_step

    return expanded


# ---------------------------------------------------------------------------
# Available machine query
# ---------------------------------------------------------------------------

def get_available_machines() -> list[dict[str, Any]]:
    """
    Return all currently available machines, marking stale physical desktops
    as idle first. This is the single authoritative place that applies the
    heartbeat timeout rule.
    """
    from infrastructure.db import execute, query_all

    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=MACHINE_STALE_SECONDS)
    ).isoformat()

    execute(
        """
        UPDATE machines
        SET status = 'idle'
        WHERE status = 'available'
          AND machine_type NOT IN ('runpod_serverless', 'modal_serverless', 'vast_serverless')
          AND (last_seen_at IS NULL OR last_seen_at < %s)
        """,
        (cutoff,),
    )
    rows = query_all(
        """
        SELECT * FROM machines
        WHERE status = 'available'
          AND (
            machine_type IN ('runpod_serverless', 'modal_serverless', 'vast_serverless')
            OR last_seen_at >= %s
          )
        ORDER BY gpu_vram_gb DESC
        """,
        (cutoff,),
    )
    return filter_enabled_machines(rows)


def choose_retry_machine(
    group_id: str,
    failed_machine_id: str,
) -> str | None:
    """
    Pick the best available machine from the pool, excluding the one that failed.
    Prefers serverless endpoints (instant spin-up).
    Falls back to the failed machine itself if nothing else is available.
    """
    from infrastructure.db import query_all

    rows = query_all(
        "SELECT * FROM machines WHERE status = 'available' AND id != %s ORDER BY gpu_vram_gb DESC",
        (failed_machine_id,),
    )
    rows = filter_enabled_machines(rows)
    if not rows:
        return failed_machine_id
    serverless = [r for r in rows if r.get("machine_type") in SERVERLESS_TYPES]
    return serverless[0]["id"] if serverless else rows[0]["id"]
