"""Modal startup recovery — reattach monitor threads for in-flight jobs."""

from __future__ import annotations

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
        # Recovery only ever runs against rows that were dispatched —
        # group_id, modal_function_call_id, render_overrides_json, and
        # input_filename are all populated by dispatch.  If any are
        # missing the row is corrupt; fail loud rather than fabricate
        # placeholders (the previous "modal-{job_id[:12]}" placeholder
        # silently masked monitor failures because the fake call-id
        # never matched anything in Modal's API).
        group_id = row.get("group_id")
        if not group_id:
            raise RuntimeError(f"Modal recovery: job {job_id} has no group_id")
        provider_job_id = (row.get("modal_function_call_id") or "").strip()
        if not provider_job_id:
            raise RuntimeError(
                f"Modal recovery: job {job_id} has no modal_function_call_id "
                "(dispatch likely crashed before save_provider_job_id ran)"
            )
        input_filename = row.get("rg_input_filename") or row.get("input_filename")
        if not input_filename:
            raise RuntimeError(
                f"Modal recovery: job {job_id} has no input filename "
                "(neither render_groups.input_filename nor jobs.input_filename)"
            )
        overrides_json = row.get("render_overrides_json")
        if not overrides_json:
            raise RuntimeError(
                f"Modal recovery: job {job_id} has no render_overrides_json"
            )
        blend_url = f"{self._cfg.public_backend_url}/render-groups/{group_id}/input/{input_filename}"

        self._callback_handler.start_monitoring(
            job_id=job_id,
            provider_job_id=provider_job_id,
            machine_id=row["machine_id"],
            blend_url=blend_url,
            render_overrides_json=overrides_json,
            group_id=group_id,
        )
