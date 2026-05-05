"""RenderGroupRepository — all SQL for the ``render_groups`` table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from serverV2.infrastructure.db import execute, query_all, query_one


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RenderGroupRepository:

    def get_by_id(self, group_id: str) -> dict[str, Any] | None:
        return query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))

    def get_resolved_heaviness(self, group_id: str) -> dict[str, Any]:
        """Return the heaviness sub-dict from the merged scene blob the
        RenderGroupService persisted at confirm-upload time.  This is
        the canonical scene-context the allocation planner reads for
        cost / time / VRAM math; both initial and retry pending rows
        load it via this method.

        Returns ``{}`` when the group is unknown or the row's
        ``resolved_scene_json`` is empty/malformed.  An empty dict is
        a degraded but safe input for the planner (defaults applied)."""
        row = query_one(
            "SELECT resolved_scene_json FROM render_groups WHERE id = %s",
            (group_id,),
        )
        if row is None:
            return {}
        raw = row.get("resolved_scene_json")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        heaviness = parsed.get("heaviness")
        return heaviness if isinstance(heaviness, dict) else {}

    def update_status(self, group_id: str, status: str) -> None:
        if status in ("done", "failed", "cancelled"):
            execute(
                "UPDATE render_groups SET status = %s, completed_at = %s WHERE id = %s",
                (status, _now_iso(), group_id),
            )
        else:
            execute("UPDATE render_groups SET status = %s WHERE id = %s", (status, group_id))

    def update_terminal_snapshot(
        self,
        group_id: str,
        *,
        tasks_count: int,
        latest_output_file: str | None,
        latest_output_job_id: str | None,
        available_output_files_count: int,
        overall_rendered_frames: int,
    ) -> None:
        """Persist the per-group fields that the list view needs but that
        today are recomputed from children on every refresh.  Called once
        per group, when it enters a terminal state — frees the list endpoint
        from having to re-fetch jobs+machines for finished groups."""
        execute(
            """
            UPDATE render_groups
            SET tasks_count = %s,
                latest_output_file = %s,
                latest_output_job_id = %s,
                available_output_files_count = %s,
                overall_rendered_frames = %s
            WHERE id = %s
            """,
            (
                tasks_count,
                latest_output_file,
                latest_output_job_id,
                available_output_files_count,
                overall_rendered_frames,
                group_id,
            ),
        )

    def update_frames(
        self, group_id: str, *, total_frames: int,
        frame_start: int, frame_end: int, frame_step: int,
    ) -> None:
        execute(
            """
            UPDATE render_groups
            SET total_frames = %s, frame_start = %s, frame_end = %s, frame_step = %s
            WHERE id = %s
            """,
            (total_frames, frame_start, frame_end, frame_step, group_id),
        )

    def create(
        self,
        *,
        group_id: str,
        input_filename: str,
        r2_input_key: str,
        status: str,
        user_id: str | None = None,
        source_asset_id: str | None = None,
    ) -> None:
        execute(
            """
            INSERT INTO render_groups (
                id, input_filename, r2_input_key, total_frames,
                frame_start, frame_end, frame_step, status,
                submitted_at, user_id, source_asset_id
            )
            VALUES (%s, %s, %s, 0, 1, 1, 1, %s, %s, %s, %s)
            """,
            (group_id, input_filename, r2_input_key, status, _now_iso(), user_id, source_asset_id),
        )

    def full_update(self, group_id: str, **fields: Any) -> None:
        if not fields:
            return
        set_clauses = []
        values: list[Any] = []
        for col, val in fields.items():
            set_clauses.append(f"{col} = %s")
            values.append(val)
        values.append(group_id)
        execute(
            f"UPDATE render_groups SET {', '.join(set_clauses)} WHERE id = %s",
            tuple(values),
        )

    def delete(self, group_id: str) -> None:
        execute("DELETE FROM jobs WHERE group_id = %s", (group_id,))
        execute("DELETE FROM render_groups WHERE id = %s", (group_id,))

    def get_by_user(self, user_id: str) -> list[dict[str, Any]]:
        """Full rows — the list endpoint reads snapshot columns straight off
        the row for terminal groups and only fetches children for the
        active ones."""
        return query_all(
            "SELECT * FROM render_groups WHERE user_id = %s ORDER BY submitted_at DESC",
            (user_id,),
        )

    def get_active_groups(self) -> list[dict[str, Any]]:
        return query_all("SELECT * FROM render_groups WHERE status IN ('pending', 'running')")

    def get_terminal_groups(self) -> list[dict[str, Any]]:
        return query_all("SELECT * FROM render_groups WHERE status IN ('cancelled', 'failed')")
