"""Vast.ai serverless provision strategy."""

from __future__ import annotations


class VastStrategy:

    @property
    def machine_type(self) -> str:
        return "vast_serverless"

    def is_enabled(self) -> bool:
        from services import vast_dispatch
        return vast_dispatch.is_enabled()

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
        from services import vast_dispatch

        instance_id = vast_dispatch.dispatch_and_save(
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_b64=render_overrides_b64,
            machine_id=machine_id,
        )
        vast_dispatch.start_polling_thread(
            job_id=job_id,
            instance_id=instance_id,
            machine_id=machine_id,
            blend_url=blend_url,
            render_overrides_b64=render_overrides_b64,
            group_id=group_id,
        )

    def cancel(self, provider_job_id: str, machine_id: str) -> None:
        from services import vast_dispatch
        vast_dispatch.cancel_job(provider_job_id, machine_id)

    @property
    def workers_per_endpoint(self) -> int:
        from services import vast_dispatch
        return vast_dispatch.VAST_WORKERS_PER_ENDPOINT

    @property
    def min_frames_per_instance(self) -> int:
        return 25
