"""
RenderJob — per-job lifecycle dataclass.

Job status lifecycle:
    pending -> running -> done | failed | cancelled
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from models.value_objects import SERVERLESS_TYPES


_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


@dataclass(frozen=True)
class RenderJob:
    job_id: str
    group_id: str
    machine_id: str
    machine_type: str
    status: str
    frame_start: int
    frame_end: int
    frame_step: int
    rendered_frames: int
    total_frames: int
    attempt: int
    max_retries: int
    submitted_at: str
    last_heartbeat_at: str | None
    input_filename: str
    render_overrides_json: str | None
    chunk_index: int | None
    chunk_size_frames: int | None
    priority: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RenderJob:
        machine_type = row.get("machine_type", "")
        if not machine_type:
            machine_type = "windows"
        return cls(
            job_id=row["id"],
            group_id=row.get("group_id", ""),
            machine_id=row.get("machine_id", ""),
            machine_type=machine_type,
            status=row.get("status", "pending"),
            frame_start=row.get("frame_start") or 0,
            frame_end=row.get("frame_end") or 0,
            frame_step=row.get("frame_step") or 1,
            rendered_frames=max(0, row.get("rendered_frames") or 0),
            total_frames=row.get("total_frames") or 0,
            attempt=row.get("attempt") or 0,
            max_retries=row.get("max_retries") or 0,
            submitted_at=row.get("submitted_at") or "",
            last_heartbeat_at=row.get("last_heartbeat_at"),
            input_filename=row.get("input_filename", ""),
            render_overrides_json=row.get("render_overrides_json"),
            chunk_index=row.get("chunk_index"),
            chunk_size_frames=row.get("chunk_size_frames"),
            priority=row.get("priority") or 0,
        )

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def is_serverless(self) -> bool:
        return self.machine_type in SERVERLESS_TYPES

    def remaining_frames(self) -> tuple[int, int] | None:
        """Return ``(new_start, frame_end)`` for the unrendered tail, or None if complete."""
        new_start = self.frame_start + self.rendered_frames * self.frame_step
        if new_start > self.frame_end:
            return None
        return (new_start, self.frame_end)

    def can_retry_same_endpoint(self) -> bool:
        return self.attempt < self.max_retries

    def is_complete_by_frames(self) -> bool:
        return self.total_frames > 0 and self.rendered_frames >= self.total_frames

    def is_heartbeat_dead(self, grace_sec: float, timeout_sec: float) -> bool:
        if not self.submitted_at:
            return False

        now = datetime.now(timezone.utc)
        try:
            submitted = datetime.fromisoformat(
                self.submitted_at.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            return False

        elapsed = (now - submitted).total_seconds()
        if elapsed < grace_sec:
            return False

        if self.last_heartbeat_at is None:
            return True

        try:
            hb_time = datetime.fromisoformat(
                str(self.last_heartbeat_at).replace("Z", "+00:00")
            )
            age = (now - hb_time).total_seconds()
            return age > timeout_sec
        except (ValueError, TypeError):
            return False

    def is_stuck_pending(self, cutoff_iso: str) -> bool:
        if self.status != "pending":
            return False
        if not self.is_serverless():
            return False
        if not self.submitted_at:
            return False
        return self.submitted_at < cutoff_iso
