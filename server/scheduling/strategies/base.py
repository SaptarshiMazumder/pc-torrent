"""Base protocol for provision strategies."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProvisionStrategy(Protocol):
    """Each provider (RunPod, Modal, community desktop) implements this."""

    @property
    def machine_type(self) -> str:
        """The machine_type value this strategy handles."""
        ...

    def is_enabled(self) -> bool:
        """Whether this provider is configured and ready to accept jobs."""
        ...

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
        """Dispatch a job to this provider and start its monitoring loop."""
        ...

    def cancel(self, provider_job_id: str, machine_id: str) -> None:
        """Cancel a running job on this provider."""
        ...

    @property
    def workers_per_endpoint(self) -> int:
        """How many parallel workers to expand each assignment into."""
        ...
