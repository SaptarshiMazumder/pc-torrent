"""UserInputFileRepository — SQL for the ``user_input_files`` table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from serverV2.infrastructure.db import execute, query_all, query_one


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserInputFileRepository:

    def upsert(
        self,
        *,
        user_id: str,
        input_filename: str,
        r2_key: str,
        frame_start: int | None = None,
        frame_end: int | None = None,
        frame_step: int | None = None,
        analysis_snapshot_json: str = "{}",
        render_overrides_json: str = "{}",
        scheduling_json: str = "{}",
        used_at: str | None = None,
    ) -> None:
        ts = used_at or _now_iso()
        execute(
            """
            INSERT INTO user_input_files (
                id, user_id, display_name, input_filename, r2_key,
                frame_start, frame_end, frame_step,
                analysis_snapshot_json, render_overrides_json, scheduling_json,
                created_at, updated_at, last_used_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, r2_key) DO UPDATE
            SET input_filename = EXCLUDED.input_filename,
                frame_start = EXCLUDED.frame_start,
                frame_end = EXCLUDED.frame_end,
                frame_step = EXCLUDED.frame_step,
                analysis_snapshot_json = EXCLUDED.analysis_snapshot_json,
                render_overrides_json = EXCLUDED.render_overrides_json,
                scheduling_json = EXCLUDED.scheduling_json,
                updated_at = EXCLUDED.updated_at,
                last_used_at = EXCLUDED.last_used_at
            """,
            (
                str(uuid4()), user_id, input_filename, input_filename, r2_key,
                frame_start, frame_end, frame_step,
                analysis_snapshot_json, render_overrides_json, scheduling_json,
                ts, ts, ts,
            ),
        )

    def list_by_user(self, user_id: str) -> list[dict[str, Any]]:
        return query_all(
            "SELECT * FROM user_input_files WHERE user_id = %s ORDER BY updated_at DESC, created_at DESC",
            (user_id,),
        )

    def get_by_id(self, asset_id: str, user_id: str) -> dict[str, Any] | None:
        return query_one(
            "SELECT * FROM user_input_files WHERE id = %s AND user_id = %s",
            (asset_id, user_id),
        )

    def rename(self, asset_id: str, display_name: str) -> None:
        execute(
            "UPDATE user_input_files SET display_name = %s, updated_at = %s WHERE id = %s",
            (display_name, _now_iso(), asset_id),
        )

    def delete(self, asset_id: str, user_id: str) -> None:
        execute(
            "DELETE FROM user_input_files WHERE id = %s AND user_id = %s",
            (asset_id, user_id),
        )

    def backfill_from_groups(self, user_id: str) -> None:
        groups = query_all(
            """
            SELECT input_filename, r2_input_key, frame_start, frame_end, frame_step,
                   analysis_snapshot_json, render_overrides_json, scheduling_json, submitted_at
            FROM render_groups
            WHERE user_id = %s AND status != 'uploading'
              AND r2_input_key IS NOT NULL AND r2_input_key != ''
            ORDER BY submitted_at DESC
            """,
            (user_id,),
        )
        for g in groups:
            self.upsert(
                user_id=user_id,
                input_filename=g.get("input_filename") or "input.blend",
                r2_key=g.get("r2_input_key") or "",
                frame_start=g.get("frame_start"),
                frame_end=g.get("frame_end"),
                frame_step=g.get("frame_step"),
                analysis_snapshot_json=g.get("analysis_snapshot_json") or "{}",
                render_overrides_json=g.get("render_overrides_json") or "{}",
                scheduling_json=g.get("scheduling_json") or "{}",
                used_at=g.get("submitted_at") or _now_iso(),
            )

    def has_active_references(self, r2_key: str, user_id: str) -> bool:
        row = query_one(
            """
            SELECT COUNT(*) AS cnt FROM render_groups
            WHERE r2_input_key = %s AND user_id = %s
              AND status IN ('uploading', 'pending', 'running')
            """,
            (r2_key, user_id),
        )
        return (row or {}).get("cnt", 0) > 0
