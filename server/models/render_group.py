"""
RenderGroup — group lifecycle dataclass.

Status lifecycle:
    uploading -> pending -> running -> done | failed
    (cancelled can be set from any non-terminal state by user action)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


@dataclass(frozen=True)
class RenderGroup:
    group_id: str
    status: str
    total_frames: int
    frame_start: int
    frame_end: int
    frame_step: int
    input_filename: str
    r2_input_key: str | None
    submitted_at: str
    completed_at: str | None
    error: str | None
    user_id: str
    source_asset_id: str | None
    render_overrides_json: str | None
    scheduling_json: str | None
    analysis_snapshot_json: str | None
    analysis_warnings_json: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RenderGroup:
        return cls(
            group_id=row["id"],
            status=row.get("status", "pending"),
            total_frames=row.get("total_frames") or 0,
            frame_start=row.get("frame_start") or 1,
            frame_end=row.get("frame_end") or 1,
            frame_step=row.get("frame_step") or 1,
            input_filename=row.get("input_filename", ""),
            r2_input_key=row.get("r2_input_key"),
            submitted_at=row.get("submitted_at", ""),
            completed_at=row.get("completed_at"),
            error=row.get("error"),
            user_id=row.get("user_id", ""),
            source_asset_id=row.get("source_asset_id"),
            render_overrides_json=row.get("render_overrides_json"),
            scheduling_json=row.get("scheduling_json"),
            analysis_snapshot_json=row.get("analysis_snapshot_json"),
            analysis_warnings_json=row.get("analysis_warnings_json"),
        )

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def can_dispatch(self) -> bool:
        return self.status == "pending"

    def can_cancel(self) -> bool:
        return self.status not in ("done", "cancelled")

    def can_rerender(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def needs_upload(self) -> bool:
        return self.status == "uploading"

    def should_check_failover(self) -> bool:
        return self.status in ("pending", "running")

    def has_orphaned_jobs(self) -> bool:
        return self.status in ("cancelled", "failed")
