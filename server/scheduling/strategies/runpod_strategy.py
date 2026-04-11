"""RunPod serverless provision strategy."""

from __future__ import annotations


class RunPodStrategy:

    @property
    def machine_type(self) -> str:
        return "runpod_serverless"

    def is_enabled(self) -> bool:
        from services import runpod_dispatch
        return runpod_dispatch.is_enabled()

    def dispatch(
        self,
        *,
        job_id: str,
        machine_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_b64: str,
        group_id: str,
    ) -> None:
        from services import runpod_autoscaler, runpod_dispatch

        endpoint_id = runpod_dispatch.endpoint_id_for_machine(machine_id)

        # Scale up the endpoint before dispatching
        try:
            runpod_autoscaler.scale_up(endpoint_id)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                f"Autoscaler scale_up FAILED for {endpoint_id}: {exc}"
            )

        runpod_autoscaler.notify_job_started(job_id, endpoint_id)

        rp_job_id = runpod_dispatch.dispatch_and_save(
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
            machine_id=machine_id,
        )
        runpod_dispatch.start_polling_thread(
            job_id=job_id,
            runpod_job_id=rp_job_id,
            machine_id=machine_id,
            blend_url=blend_url,
            render_overrides_b64=render_overrides_b64,
            group_id=group_id,
        )

    def cancel(self, provider_job_id: str, machine_id: str) -> None:
        from services import runpod_dispatch
        runpod_dispatch.cancel_job(provider_job_id, machine_id)

    def provider_job_id_from_job(self, job: dict) -> str | None:
        value = (job.get("runpod_job_id") or "").strip()
        return value or None

    @property
    def workers_per_endpoint(self) -> int:
        from scheduling.frame_distributor import WORKERS_PER_SERVERLESS
        return WORKERS_PER_SERVERLESS

    @property
    def min_frames_per_instance(self) -> int:
        return 10
