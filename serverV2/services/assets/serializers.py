"""Pure formatting helpers for user-input-file (asset) responses."""

from __future__ import annotations

from typing import Any

from serverV2.core.value_objects import normalize_render_overrides, parse_json_object


def serialize_asset(row: dict[str, Any]) -> dict[str, Any]:
    """Raw DB row -> API-facing asset DTO.

    Parses stored JSON blobs into objects, normalizes render overrides, and
    builds a ready-to-prefill frame range from the saved defaults.
    """
    frame_start = row.get("frame_start")
    frame_end = row.get("frame_end")
    frame_step = row.get("frame_step")

    prefill_frame_range: dict[str, int] | None = None
    if frame_start is not None and frame_end is not None:
        prefill_frame_range = {
            "frame_start": int(frame_start),
            "frame_end": int(frame_end),
            "frame_step": int(frame_step or 1),
        }

    return {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "display_name": row.get("display_name"),
        "input_filename": row.get("input_filename"),
        "r2_key": row.get("r2_key"),
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_step": frame_step,
        "analysis_snapshot": parse_json_object(row.get("analysis_snapshot_json"), {}),
        "render_overrides": normalize_render_overrides(
            parse_json_object(row.get("render_overrides_json"), {})
        ),
        "scheduling": parse_json_object(row.get("scheduling_json"), {}),
        "prefill_frame_range": prefill_frame_range,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_used_at": row.get("last_used_at"),
    }
