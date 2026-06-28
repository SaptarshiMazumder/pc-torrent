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

    def set_pre_render_cost_estimate_usd(
        self, group_id: str, value: float,
    ) -> None:
        """Stamp the LLM-derived cost projection taken at submit time.
        Frozen for the life of the group -- retries do NOT update.
        """
        execute(
            "UPDATE render_groups SET pre_render_cost_estimate_usd = %s "
            "WHERE id = %s",
            (float(value), group_id),
        )

    def get_pre_render_cost_estimate_usd(self, group_id: str) -> float | None:
        """Read the LLM-derived projection stamped at submit time.
        ``None`` for legacy groups submitted before the column existed
        -- callers fall back to SUM(jobs.estimated_cost_usd) in that case.
        """
        row = query_one(
            "SELECT pre_render_cost_estimate_usd FROM render_groups "
            "WHERE id = %s",
            (group_id,),
        )
        if row is None:
            return None
        raw = row.get("pre_render_cost_estimate_usd")
        return float(raw) if raw is not None else None

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
        total_actual_cost_usd: float | None,
    ) -> None:
        """Persist the per-group fields that the list view needs but that
        today are recomputed from children on every refresh.  Called once
        per group, when it enters a terminal state — frees the list endpoint
        from having to re-fetch jobs+machines for finished groups.

        ``total_actual_cost_usd`` is the priority-adjusted actual cost
        rolled up across the group's chunks (stored in USD; the service
        projects it to credits at the wire boundary, matching the
        estimated-cost column)."""
        execute(
            """
            UPDATE render_groups
            SET tasks_count = %s,
                latest_output_file = %s,
                latest_output_job_id = %s,
                available_output_files_count = %s,
                overall_rendered_frames = %s,
                total_actual_cost_usd = %s
            WHERE id = %s
            """,
            (
                tasks_count,
                latest_output_file,
                latest_output_job_id,
                available_output_files_count,
                overall_rendered_frames,
                total_actual_cost_usd,
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

    def get_by_user_page(
        self, user_id: str, *, limit: int, offset: int,
        status_group: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Paginated slice of a user's groups, newest-first.  Returns
        ``(rows, has_more)``.  ``has_more`` is detected by selecting one
        extra row beyond ``limit`` -- avoids a second ``COUNT(*)`` query.

        ``status_group`` filters the slice to either active
        (uploading/pending/running) or terminal (done/failed/cancelled)
        groups.  ``None`` returns the full mixed list for backwards
        compatibility.

        Tie-break on ``id DESC`` so groups submitted in the same second
        keep a stable order across pages.
        """
        params: list[Any] = [user_id]
        where = "user_id = %s"
        if status_group == "active":
            where += " AND status IN ('uploading', 'pending', 'running')"
        elif status_group == "terminal":
            where += " AND status IN ('done', 'failed', 'cancelled')"
        params.extend([limit + 1, offset])
        rows = query_all(
            f"SELECT * FROM render_groups WHERE {where} "
            "ORDER BY submitted_at DESC, id DESC LIMIT %s OFFSET %s",
            tuple(params),
        )
        has_more = len(rows) > limit
        return rows[:limit], has_more

    def get_active_groups(self) -> list[dict[str, Any]]:
        return query_all("SELECT * FROM render_groups WHERE status IN ('pending', 'running')")

    def get_terminal_groups(self) -> list[dict[str, Any]]:
        return query_all("SELECT * FROM render_groups WHERE status IN ('cancelled', 'failed')")
