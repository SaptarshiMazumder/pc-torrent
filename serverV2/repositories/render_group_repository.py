"""RenderGroupRepository — all SQL for the ``render_groups`` table."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from serverV2.infrastructure.db import execute, query_all, query_one


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RenderGroupRepository:

    def get_by_id(self, group_id: str) -> dict[str, Any] | None:
        return query_one("SELECT * FROM render_groups WHERE id = %s", (group_id,))

    def update_status(self, group_id: str, status: str) -> None:
        if status in ("done", "failed", "cancelled"):
            execute(
                "UPDATE render_groups SET status = %s, completed_at = %s WHERE id = %s",
                (status, _now_iso(), group_id),
            )
        else:
            execute("UPDATE render_groups SET status = %s WHERE id = %s", (status, group_id))

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
        return query_all(
            "SELECT id FROM render_groups WHERE user_id = %s ORDER BY submitted_at DESC",
            (user_id,),
        )

    def get_active_groups(self) -> list[dict[str, Any]]:
        return query_all("SELECT * FROM render_groups WHERE status IN ('pending', 'running')")

    def get_terminal_groups(self) -> list[dict[str, Any]]:
        return query_all("SELECT * FROM render_groups WHERE status IN ('cancelled', 'failed')")
