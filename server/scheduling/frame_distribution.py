"""
Frame distribution — scoring, budgeting, splitting, and serverless expansion.

These functions need access to scheduling.strategies (to check is_enabled,
min_frames_per_instance, workers_per_endpoint) so they live in the scheduling
layer, not domain.
"""

from __future__ import annotations

import os
from typing import Any

from models.value_objects import MIN_FRAMES_PER_WORKER, SERVERLESS_TYPES
from scheduling.strategies import get_strategy

try:
    WORKERS_PER_SERVERLESS = max(1, int(os.getenv("WORKERS_PER_SERVERLESS", "3")))
except ValueError:
    WORKERS_PER_SERVERLESS = 3


# ---------------------------------------------------------------------------
# Machine scoring
# ---------------------------------------------------------------------------

def compute_power_score(machine: dict[str, Any]) -> float:
    speed = machine.get("render_speed") or 1.0
    vram = machine.get("gpu_vram_gb") or 0
    cores = machine.get("cpu_cores") or 0
    ram = machine.get("ram_gb") or 0
    base = (vram * 2) + (cores * 0.5) + (ram * 0.2)
    return base * speed


def filter_enabled_machines(machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop machines whose provision strategy is currently disabled."""
    return [m for m in machines if get_strategy(m.get("machine_type", "windows")).is_enabled()]


# ---------------------------------------------------------------------------
# Worker budget capping
# ---------------------------------------------------------------------------

def max_workers_for_frame_budget(
    total_frames: int,
    requested_workers: int,
    min_frames: int = MIN_FRAMES_PER_WORKER,
) -> int:
    if requested_workers <= 1:
        return 1
    if total_frames <= 0:
        return 1
    return max(1, min(requested_workers, total_frames // min_frames))


def _min_frames_for_machine(machine: dict[str, Any]) -> int:
    strategy = get_strategy(machine.get("machine_type", "windows"))
    return getattr(strategy, "min_frames_per_instance", MIN_FRAMES_PER_WORKER)


def limit_machines_for_frame_budget(
    machines: list[dict[str, Any]],
    total_frames: int,
) -> list[dict[str, Any]]:
    """Keep only as many machines as the frame budget can justify."""
    if not machines:
        return []
    ranked = sorted(machines, key=compute_power_score, reverse=True)
    kept: list[dict[str, Any]] = []
    remaining = total_frames
    for machine in ranked:
        min_frames = _min_frames_for_machine(machine)
        if remaining >= min_frames:
            kept.append(machine)
            remaining -= min_frames
    return kept if kept else ranked[:1]


# ---------------------------------------------------------------------------
# Frame distribution
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
    expanded: list[dict[str, Any]] = []
    for a in assignments:
        mt = a.get("machine_type", "")
        if mt not in SERVERLESS_TYPES:
            expanded.append(a)
            continue

        strategy = get_strategy(mt)
        effective_workers = strategy.workers_per_endpoint
        if effective_workers <= 1:
            expanded.append(a)
            continue

        total_frames = a["total_frames"]
        if total_frames <= 1:
            expanded.append(a)
            continue

        min_frames = getattr(strategy, "min_frames_per_instance", MIN_FRAMES_PER_WORKER)
        worker_count = max_workers_for_frame_budget(total_frames, effective_workers, min_frames)
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
