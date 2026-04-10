"""Community (physical desktop) provision strategy.

Desktop agents poll the server for pending jobs, so dispatch is a no-op.
"""

from __future__ import annotations


class CommunityStrategy:

    @property
    def machine_type(self) -> str:
        return "windows"

    def is_enabled(self) -> bool:
        return True

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
        pass

    def cancel(self, provider_job_id: str, machine_id: str) -> None:
        pass

    @property
    def workers_per_endpoint(self) -> int:
        return 1

    @property
    def min_frames_per_instance(self) -> int:
        return 2
