"""AllowedStallTimesResolver -- resolves per-job allowed-stall-time
deadlines at dispatch.

Single source of truth for the clamp/multiplier math.  The output is
written verbatim to ``jobs.allowed_stall_times`` (JSONB column); both
the fleet singletons (per-tick stall checks) and the UI (instance
card) read from that same column.  No drift possible because the math
runs exactly once per chunk-attempt, at dispatch.

All values are seconds.  Fleet-specific keys (``startup_timeout_sec``,
``in_queue_timeout_sec``) are emitted only for the relevant fleet;
shared keys are present on every fleet's row.

Configuration flow: reads its config fresh from Firestore (via
RenderConfigRepository) at the top of every ``resolve()`` call -- the
``stall`` block plus the per-fleet timeout knobs
(``vast.startup_timeout_sec``, ``vast.heartbeat_grace_sec``,
``modal.in_queue_timeout_sec``, ``monitor.in_progress_stale_sec``).
Editing any of them in the admin UI takes effect on the NEXT chunk
dispatch -- no redeploy.  Nothing is injected at boot.
"""

from __future__ import annotations

import json
from typing import Any

from serverV2.config.render_config_repository import (
    RenderConfigRepository,
)
from serverV2.config.render_config import StallLoadingSafetyConfig
from serverV2.core.value_objects import parse_analysis_heaviness
from serverV2.repositories.render_group_repository import RenderGroupRepository

_GIB = 1024 ** 3
_MODAL_HEARTBEAT_GRACE_SEC = 90.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class AllowedStallTimesResolver:

    def __init__(
        self,
        *,
        config_repo: RenderConfigRepository,
        group_repo: RenderGroupRepository,
    ) -> None:
        self._config_repo = config_repo
        self._group_repo = group_repo

    def resolve(
        self,
        *,
        fleet: str,
        group_id: str,
    ) -> dict[str, Any]:
        """Compute the deadline dict for a chunk being dispatched.

        Reads stall config fresh from Firestore each call.  Pulls the
        scene heaviness + file size from the group row (one DB read
        for both) to compute the loading-safety window.
        """
        cfg = self._config_repo.get()
        stall = cfg.stall
        ls = stall.loading_safety

        heaviness, size_bytes = self._load_group_context(group_id)

        # Loading-safety window: separate from the cost analyzer's
        # startup_sec.  Same shape (file/verts/textures/shader nodes +
        # per-fleet provisioning) but its own coefficients tuned for a
        # permissive deadline.
        est_loading_safe = self._compute_loading_safe(heaviness, fleet, ls)
        loading_stall = _clamp(
            est_loading_safe * stall.loading_multiplier,
            stall.loading_phase_min_sec,
            stall.loading_phase_max_sec,
        )

        # Download-phase-max: clamp(size_gb * secs_per_gb, min, max).
        # When file size unknown (legacy row, etc.), fall back to
        # max_sec -- matches DownloadCeilingRule's pre-refactor fallback.
        if size_bytes > 0:
            download_phase_max = _clamp(
                (size_bytes / _GIB) * stall.download_secs_per_gb,
                stall.download_phase_min_sec,
                stall.download_phase_max_sec,
            )
        else:
            download_phase_max = stall.download_phase_max_sec

        out: dict[str, Any] = {
            "loading_stall_sec": loading_stall,
            "download_phase_max_sec": download_phase_max,
            "download_bytes_stall_sec": stall.download_bytes_stall_sec,
            "hard_ceiling_sec": stall.hard_max_chunk_sec,
            "frame_progress_stale_sec": cfg.monitor.in_progress_stale_sec,
        }

        if fleet == "vast_serverless":
            out["startup_timeout_sec"] = cfg.vast.startup_timeout_sec
            out["heartbeat_grace_sec"] = cfg.vast.heartbeat_grace_sec
        elif fleet == "modal_serverless":
            out["in_queue_timeout_sec"] = cfg.modal.in_queue_timeout_sec
            out["heartbeat_grace_sec"] = _MODAL_HEARTBEAT_GRACE_SEC

        return out

    @staticmethod
    def _compute_loading_safe(
        heaviness: dict[str, Any], fleet: str,
        ls: StallLoadingSafetyConfig,
    ) -> float:
        """Per-heaviness safe-loading estimate in seconds.

        Mirrors estimate_startup_seconds' shape but uses the
        stall-specific coefficients from StallLoadingSafetyConfig --
        intentionally pessimistic so the watchdog window doesn't
        shrink when the cost analyzer is re-calibrated.
        """
        fleet_buffer_map = {
            "vast_serverless":  ls.fleet_buffer_vast,
            "modal_serverless": ls.fleet_buffer_modal,
            "community":        ls.fleet_buffer_community,
        }
        fleet_buffer = fleet_buffer_map.get(fleet, 0.0)

        file_gb = float(heaviness.get("file_size_bytes") or 0) / _GIB
        verts_m = float(heaviness.get("vertex_count_total") or 0) / 1_000_000
        tex_gb = float(heaviness.get("texture_total_bytes") or 0) / _GIB
        nodes = float(heaviness.get("shader_node_count_total") or 0)

        return (
            ls.baseline_sec
            + file_gb * ls.per_gb_file
            + verts_m * ls.per_million_verts
            + tex_gb * ls.per_gb_texture
            + nodes * ls.per_shader_node
            + fleet_buffer
        )

    def _load_group_context(self, group_id: str) -> tuple[dict[str, Any], int]:
        """One DB read; returns (heaviness dict, file_size_bytes).

        Both come from the same render_groups row; combining the lookup
        avoids the two separate calls the old resolver had.
        """
        if not group_id:
            return {}, 0
        try:
            row = self._group_repo.get_by_id(group_id)
        except Exception:
            return {}, 0
        if not row:
            return {}, 0

        size_bytes = 0
        size = row.get("r2_input_size_bytes")
        try:
            size_bytes = int(size) if size is not None else 0
        except (TypeError, ValueError):
            size_bytes = 0

        heaviness: dict[str, Any] = {}
        raw = row.get("resolved_scene_json")
        if raw:
            try:
                parsed = json.loads(raw)
                heaviness = parse_analysis_heaviness(
                    parsed, file_size_bytes=size_bytes if size_bytes > 0 else None,
                )
            except Exception:
                heaviness = {}

        return heaviness, size_bytes
