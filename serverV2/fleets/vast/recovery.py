"""Vast.ai startup recovery — reattach polling threads for in-flight jobs."""

from __future__ import annotations

import base64
import logging

from serverV2.core.value_objects import now_iso
from serverV2.infrastructure.db import execute, query_all, query_one

log = logging.getLogger(__name__)


class VastRecovery:

    def __init__(self, *, config, client, callback_handler) -> None:
        self._cfg = config
        self._client = client
        self._callback_handler = callback_handler

    def recover(self) -> None:
        if not self._cfg.is_enabled():
            return
        self._reattach_inflight()
        self._cleanup_orphaned()
        log.info("Vast recovery: done")

    def _reattach_inflight(self) -> None:
        rows = query_all(
            """
            SELECT j.id, j.runpod_job_id, j.machine_id, j.group_id,
                   j.render_overrides_json, j.input_filename,
                   rg.input_filename AS rg_input_filename
            FROM jobs j
            LEFT JOIN render_groups rg ON rg.id = j.group_id
            WHERE j.status IN ('running', 'pending')
              AND j.machine_id IN (
                  SELECT id FROM machines WHERE machine_type = 'vast_serverless'
              )
              AND j.runpod_job_id IS NOT NULL
            """,
        )
        if not rows:
            log.info("Vast recovery: no in-flight jobs to recover")
            return

        log.info("Vast recovery: found %d in-flight job(s)", len(rows))
        for row in rows:
            self._recover_single(row)

    def _recover_single(self, row: dict) -> None:
        job_id = row["id"]
        instance_id_raw = row["runpod_job_id"]
        group_id = row.get("group_id") or ""
        overrides_json = row.get("render_overrides_json") or "{}"
        overrides_b64 = base64.b64encode(overrides_json.encode()).decode()

        fname = row.get("rg_input_filename") or row.get("input_filename") or ""
        blend_url = (
            f"{self._cfg.public_backend_url}/render-groups/{group_id}/input/{fname}"
            if group_id and fname else ""
        )

        try:
            vast_id = int(instance_id_raw)
        except (ValueError, TypeError):
            log.warning("Vast recovery: job %s has invalid instance_id '%s', marking failed", job_id, instance_id_raw)
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Server restarted — instance ID invalid", now_iso(), job_id),
            )
            return

        inst = self._client.get_instance(vast_id)
        if inst is None:
            log.warning("Vast recovery: instance %d for job %s is gone — marking failed", vast_id, job_id)
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Instance gone after server restart", now_iso(), job_id),
            )
        else:
            log.info("Vast recovery: reattaching monitor for job %s instance %d", job_id, vast_id)
            self._callback_handler.start_monitoring(
                job_id=job_id,
                provider_job_id=str(vast_id),
                machine_id=row["machine_id"],
                blend_url=blend_url,
                render_overrides_b64=overrides_b64,
                group_id=group_id,
            )

    def _cleanup_orphaned(self) -> None:
        terminal_rows = query_all(
            """
            SELECT j.id, j.runpod_job_id
            FROM jobs j
            WHERE j.status IN ('done', 'cancelled', 'failed')
              AND j.runpod_job_id IS NOT NULL
              AND j.completed_at::timestamptz > NOW() - INTERVAL '2 hours'
              AND j.machine_id IN (
                  SELECT id FROM machines WHERE machine_type = 'vast_serverless'
              )
            """,
        )
        for row in terminal_rows:
            try:
                vast_id = int(row["runpod_job_id"])
                inst = self._client.get_instance(vast_id)
                if inst is not None:
                    actual = str(inst.get("actual_status") or "").lower()
                    if actual not in ("exited", "stopped", "offline"):
                        log.info("Vast cleanup: destroying orphaned instance %d (job %s terminal)", vast_id, row["id"])
                        self._client.destroy_instance(vast_id)
            except Exception as e:
                log.warning("Vast cleanup error for instance %s: %s", row.get("runpod_job_id"), e)
