"""AllowedStallTimesResolver — resolves per-job allowed-stall-time
deadlines at dispatch.

Single source of truth for the clamp/multiplier math.  The output is
written verbatim to ``jobs.allowed_stall_times`` (JSONB column); both
the fleet singletons (per-tick stall checks) and the UI (instance
card) read from that same column.  No drift possible because the math
runs exactly once per chunk-attempt, at dispatch.

All values are seconds.  Fleet-specific keys (``startup_timeout_sec``,
``in_queue_timeout_sec``) are emitted only for the relevant fleet;
shared keys are present on every fleet's row.

The resolver holds config refs at construction so callers (the three
fleet strategies) only pass per-job inputs at call time.
"""

from __future__ import annotations

from typing import Any

from serverV2.config import ModalConfig, StallDetectionConfig, VastConfig
from serverV2.repositories.render_group_repository import RenderGroupRepository

_GIB = 1024 ** 3
_MODAL_HEARTBEAT_GRACE_SEC = 90.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class AllowedStallTimesResolver:

    def __init__(
        self,
        *,
        vast_cfg: VastConfig,
        modal_cfg: ModalConfig,
        stall_cfg: StallDetectionConfig,
        in_progress_stale_sec: float,
        group_repo: RenderGroupRepository,
    ) -> None:
        self._vast = vast_cfg
        self._modal = modal_cfg
        self._stall = stall_cfg
        self._in_progress_stale_sec = in_progress_stale_sec
        self._group_repo = group_repo

    def resolve(
        self,
        *,
        fleet: str,
        group_id: str,
        estimated_startup_seconds: float | None,
    ) -> dict[str, Any]:
        """Compute the deadline dict for a chunk being dispatched.

        Reads ``render_groups.r2_input_size_bytes`` to size the
        download-phase ceiling proportional to the blend size.  Falls
        back to ``stall_cfg.download_phase_max_sec`` when unknown.
        """
        est_startup = float(estimated_startup_seconds or 0.0)
        size_bytes = self._lookup_file_size_bytes(group_id)

        # Loading-stall: clamp(est_startup * multiplier, min, max).
        loading_stall = _clamp(
            est_startup * self._stall.loading_multiplier,
            self._stall.loading_phase_min_sec,
            self._stall.loading_phase_max_sec,
        )

        # Download-phase-max: clamp(size_gb * secs_per_gb, min, max).
        # When file size unknown (legacy row, etc.), fall back to
        # max_sec — matches DownloadCeilingRule's pre-refactor fallback.
        if size_bytes > 0:
            download_phase_max = _clamp(
                (size_bytes / _GIB) * self._stall.download_secs_per_gb,
                self._stall.download_phase_min_sec,
                self._stall.download_phase_max_sec,
            )
        else:
            download_phase_max = self._stall.download_phase_max_sec

        out: dict[str, Any] = {
            # Pre-render rules (apply before first frame uploads).
            "loading_stall_sec": loading_stall,
            "download_phase_max_sec": download_phase_max,
            "download_bytes_stall_sec": self._stall.download_bytes_stall_sec,
            # Phase-agnostic backstop.
            "hard_ceiling_sec": self._stall.hard_max_chunk_sec,
            # Post-first-frame: frame-progress staleness.
            "frame_progress_stale_sec": self._in_progress_stale_sec,
        }

        # Per-fleet keys.
        if fleet == "vast_serverless":
            out["startup_timeout_sec"] = self._vast.startup_timeout_sec
            out["heartbeat_grace_sec"] = self._vast.heartbeat_grace_sec
        elif fleet == "modal_serverless":
            out["in_queue_timeout_sec"] = self._modal.in_queue_timeout_sec
            out["heartbeat_grace_sec"] = _MODAL_HEARTBEAT_GRACE_SEC

        return out

    def _lookup_file_size_bytes(self, group_id: str) -> int:
        if not group_id:
            return 0
        try:
            row = self._group_repo.get_by_id(group_id)
        except Exception:
            return 0
        if not row:
            return 0
        size = row.get("r2_input_size_bytes")
        try:
            return int(size) if size is not None else 0
        except (TypeError, ValueError):
            return 0
