"""
StartupRecovery — reattach polling threads on server restart.

Scans the DB for Vast jobs left in running/pending state (their polling
threads died with the previous process) and either resumes watching them
or triggers failover.
"""

from __future__ import annotations

import base64
import logging

from domain.value_objects import now_iso
from infrastructure.db import execute, query_all, query_one
from services.vast.client import VastApiClient
from services.vast.config import VastConfig
from services.vast.failover import FailoverHandler
from services.vast.instance_registry import InstanceRegistry
from services.vast.poller import InstancePoller

log = logging.getLogger(__name__)


class StartupRecovery:
    def __init__(
        self,
        config: VastConfig,
        client: VastApiClient,
        registry: InstanceRegistry,
        failover: FailoverHandler,
    ) -> None:
        self._cfg = config
        self._client = client
        self._registry = registry
        self._failover = failover

    def recover(self) -> None:
        if not self._cfg.is_enabled():
            return
        self._reattach_inflight_jobs()
        self._cleanup_orphaned_instances()
        log.info("Vast recovery: done")

    def _reattach_inflight_jobs(self) -> None:
        rows = query_all(
            """
            SELECT j.id, j.runpod_job_id, j.machine_id, j.group_id,
                   j.frame_start, j.frame_end, j.frame_step,
                   j.rendered_frames, j.render_overrides_json, j.input_filename,
                   rg.input_filename AS rg_input_filename
            FROM jobs j
            LEFT JOIN render_groups rg ON rg.id = j.group_id
            WHERE j.status IN ('running', 'pending')
              AND j.machine_id IN (
                  SELECT id FROM machines WHERE machine_type = 'vast_serverless'
              )
              AND j.runpod_job_id IS NOT NULL
            """,
            (),
        )

        if not rows:
            log.info("Vast recovery: no in-flight jobs to recover")
            return

        log.info(f"Vast recovery: found {len(rows)} in-flight job(s), checking instances...")

        for row in rows:
            self._recover_single(row)

    def _recover_single(self, row: dict) -> None:
        job_id = row["id"]
        instance_id_raw = row["runpod_job_id"]
        machine_id = row["machine_id"]
        group_id = row["group_id"] or ""
        overrides_json = row.get("render_overrides_json") or "{}"
        render_overrides_b64 = base64.b64encode(overrides_json.encode()).decode()

        fname = row.get("rg_input_filename") or row.get("input_filename") or ""
        blend_url = (
            f"{self._cfg.public_backend_url}/render-groups/{group_id}/input/{fname}"
            if group_id and fname else ""
        )

        try:
            vast_id = int(instance_id_raw)
        except (ValueError, TypeError):
            log.warning(
                f"Vast recovery: job {job_id} has invalid instance_id "
                f"'{instance_id_raw}', marking failed"
            )
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Server restarted — instance ID invalid", now_iso(), job_id),
            )
            return

        inst = self._client.get_instance(vast_id)
        if inst is None:
            self._handle_gone_instance(
                job_id, vast_id, machine_id, group_id,
                blend_url, render_overrides_b64,
            )
        else:
            actual_status = str(inst.get("actual_status") or "").lower()
            log.info(
                f"Vast recovery: reattaching poll thread for job {job_id} "
                f"instance {vast_id} ({actual_status})"
            )
            poller = InstancePoller(
                job_id=job_id,
                instance_id=vast_id,
                machine_id=machine_id,
                blend_url=blend_url,
                render_overrides_b64=render_overrides_b64,
                group_id=group_id,
                client=self._client,
                registry=self._registry,
                failover=self._failover,
                config=self._cfg,
            )
            poller.start()

    def _handle_gone_instance(
        self,
        job_id: str,
        vast_id: int,
        machine_id: str,
        group_id: str,
        blend_url: str,
        render_overrides_b64: str,
    ) -> None:
        log.warning(
            f"Vast recovery: instance {vast_id} for job {job_id} is gone — marking failed"
        )
        job = query_one(
            "SELECT status, frame_start, frame_end, frame_step, rendered_frames, "
            "input_filename, render_overrides_json, chunk_index, chunk_size_frames, priority "
            "FROM jobs WHERE id = %s",
            (job_id,),
        )
        if job and blend_url:
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Instance gone after server restart", now_iso(), job_id),
            )
            self._failover.handle(
                job_id=job_id, job=job,
                error="Instance gone after server restart",
                blend_url=blend_url,
                render_overrides_b64=render_overrides_b64,
                failed_machine_id=machine_id,
                group_id=group_id,
            )
        else:
            execute(
                "UPDATE jobs SET status = 'failed', error = %s, completed_at = %s WHERE id = %s",
                ("Instance gone after server restart — no blend_url to retry", now_iso(), job_id),
            )

    def _cleanup_orphaned_instances(self) -> None:
        """Destroy instances for jobs that finished but whose instance was
        never destroyed (e.g. server crashed before cleanup)."""
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
            (),
        )
        for row in terminal_rows:
            try:
                vast_id = int(row["runpod_job_id"])
                inst = self._client.get_instance(vast_id)
                if inst is not None:
                    actual = str(inst.get("actual_status") or "").lower()
                    if actual not in ("exited", "stopped", "offline"):
                        log.info(
                            f"Vast cleanup: destroying orphaned instance {vast_id} "
                            f"(job {row['id']} is terminal)"
                        )
                        self._client.destroy_instance(vast_id)
            except Exception as e:
                log.warning(f"Vast cleanup error for instance {row.get('runpod_job_id')}: {e}")
