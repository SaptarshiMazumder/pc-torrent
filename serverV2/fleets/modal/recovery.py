"""Modal startup recovery — reattach monitor threads for in-flight jobs."""

from __future__ import annotations

import base64
import logging

from serverV2.infrastructure.db import query_all

log = logging.getLogger(__name__)


class ModalRecovery:

    def __init__(self, *, config, callback_handler) -> None:
        self._cfg = config
        self._callback_handler = callback_handler

    def recover(self) -> None:
        if not self._cfg.is_enabled():
            return
        # Phase 1: serverless jobs no longer have a machines row, so the
        # discriminator is jobs.machine_type, not a JOIN against machines.
        rows = query_all(
            """
            SELECT j.id, j.modal_function_call_id, j.machine_id, j.group_id,
                   j.input_filename, j.render_overrides_json,
                   rg.input_filename AS rg_input_filename
            FROM jobs j
            LEFT JOIN render_groups rg ON rg.id = j.group_id
            WHERE j.status IN ('running', 'pending')
              AND j.machine_type = 'modal_serverless'
            """,
        )
        if not rows:
            log.info("Modal recovery: no in-flight jobs to recover")
            return

        log.info("Modal recovery: reattaching %d in-flight monitor(s)", len(rows))
        for row in rows:
            self._recover_single(row)
        log.info("Modal recovery: done")

    def _recover_single(self, row: dict) -> None:
        job_id = row["id"]
        group_id = row.get("group_id") or ""
        provider_job_id = (row.get("modal_function_call_id") or "").strip()
        if not provider_job_id:
            provider_job_id = f"modal-{job_id[:12]}"

        input_filename = row.get("rg_input_filename") or row.get("input_filename") or ""
        if group_id and input_filename:
            blend_url = f"{self._cfg.public_backend_url}/render-groups/{group_id}/input/{input_filename}"
        elif input_filename:
            blend_url = f"{self._cfg.public_backend_url}/jobs/{job_id}/input/{input_filename}"
        else:
            blend_url = ""

        overrides_json = row.get("render_overrides_json") or "{}"
        overrides_b64 = base64.b64encode(overrides_json.encode()).decode()

        self._callback_handler.start_monitoring(
            job_id=job_id,
            provider_job_id=provider_job_id,
            machine_id=row["machine_id"],
            blend_url=blend_url,
            render_overrides_b64=overrides_b64,
            group_id=group_id,
        )
