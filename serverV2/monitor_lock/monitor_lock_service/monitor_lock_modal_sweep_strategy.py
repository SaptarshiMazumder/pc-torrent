"""MonitorLockModalSweepStrategy — orphan reclaim for Modal per-job monitors.

Same shape as the Vast strategy: query active Modal rows, ask the
manager to start_monitoring each.  Lock acquire inside the manager
decides actual ownership.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from serverV2.config import ModalConfig
from serverV2.infrastructure.db import query_all

if TYPE_CHECKING:
    from serverV2.fleets.modal.monitor import ModalMonitorManager

log = logging.getLogger(__name__)


class MonitorLockModalSweepStrategy:

    def __init__(
        self,
        *,
        modal_cfg: ModalConfig,
        modal_manager: "ModalMonitorManager",
    ) -> None:
        self._cfg = modal_cfg
        self._manager = modal_manager

    def sweep(self) -> None:
        if not self._cfg.is_enabled():
            return
        rows = query_all(
            """
            SELECT j.id, j.modal_function_call_id, j.machine_id, j.group_id,
                   j.render_overrides_json, j.input_filename,
                   rg.input_filename AS rg_input_filename
            FROM jobs j
            LEFT JOIN render_groups rg ON rg.id = j.group_id
            WHERE j.status IN ('running', 'pending')
              AND j.machine_type = 'modal_serverless'
              AND j.modal_function_call_id IS NOT NULL
            """,
        )
        for row in rows:
            self._claim_one(row)

    def _claim_one(self, row: dict) -> None:
        job_id = row["id"]
        provider_job_id = row.get("modal_function_call_id")
        if not provider_job_id:
            log.warning(
                "Sweep skip: modal job %s has no modal_function_call_id",
                job_id,
            )
            return
        group_id = row.get("group_id")
        overrides_json = row.get("render_overrides_json")
        fname = row.get("rg_input_filename") or row.get("input_filename")
        if not (group_id and overrides_json and fname):
            log.warning(
                "Sweep skip: modal job %s has incomplete dispatch artefacts",
                job_id,
            )
            return
        blend_url = (
            f"{self._cfg.public_backend_url}"
            f"/render-groups/{group_id}/input/{fname}"
        )
        self._manager.start_monitoring(
            job_id=job_id,
            provider_job_id=provider_job_id,
            machine_id=row.get("machine_id"),
            blend_url=blend_url,
            render_overrides_json=overrides_json,
            group_id=group_id,
        )
