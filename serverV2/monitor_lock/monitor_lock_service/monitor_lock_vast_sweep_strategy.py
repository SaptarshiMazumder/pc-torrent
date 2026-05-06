"""MonitorLockVastSweepStrategy — orphan reclaim for Vast per-job monitors.

Per-tick: query ``jobs`` for active Vast rows, ask the manager to
``start_monitoring`` each.  The manager's lock acquire decides whether
this instance actually spawns the thread; we just drive the call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.config import VastConfig
from serverV2.infrastructure.db import query_all

if TYPE_CHECKING:
    from serverV2.fleets.vast.monitor import VastMonitorManager

log = logging.getLogger(__name__)


class MonitorLockVastSweepStrategy:

    def __init__(
        self,
        *,
        vast_cfg: VastConfig,
        vast_manager: "VastMonitorManager",
    ) -> None:
        self._cfg = vast_cfg
        self._manager = vast_manager

    def sweep(self) -> None:
        if not self._cfg.is_enabled():
            return
        rows = query_all(
            """
            SELECT j.id, j.vast_job_id, j.machine_id, j.group_id,
                   j.render_overrides_json, j.input_filename,
                   rg.input_filename AS rg_input_filename
            FROM jobs j
            LEFT JOIN render_groups rg ON rg.id = j.group_id
            WHERE j.status IN ('running', 'pending')
              AND j.machine_type = 'vast_serverless'
              AND j.vast_job_id IS NOT NULL
            """,
        )
        for row in rows:
            self._claim_one(row)

    def _claim_one(self, row: dict) -> None:
        job_id = row["id"]
        provider_job_id = row.get("vast_job_id")
        if not provider_job_id:
            log.warning("Sweep skip: vast job %s has no vast_job_id", job_id)
            return
        group_id = row.get("group_id")
        overrides_json = row.get("render_overrides_json")
        fname = row.get("rg_input_filename") or row.get("input_filename")
        if not (group_id and overrides_json and fname):
            log.warning(
                "Sweep skip: vast job %s has incomplete dispatch artefacts",
                job_id,
            )
            return
        blend_url = (
            f"{self._cfg.public_backend_url}"
            f"/render-groups/{group_id}/input/{fname}"
        )
        self._manager.start_monitoring(
            job_id=job_id,
            provider_job_id=str(provider_job_id),
            machine_id=row.get("machine_id"),
            blend_url=blend_url,
            render_overrides_json=overrides_json,
            group_id=group_id,
            estimated_startup_sec=float(row["estimated_startup_seconds"]),
        )
