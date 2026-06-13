"""Pydantic payloads for /pre-render/* routes.

Pre-render is the stateless RPC layer: the desktop app sends the
analyzer snapshot + the user's edited overrides + payload-level frame
hints; the backend computes cost / wall-time estimates without touching
the database.
"""

from typing import Any

from pydantic import BaseModel, Field

from serverV2.core.value_objects import (
    RENDER_PRIORITY_DEFAULT,
    RENDER_PRIORITY_HIGH,
    RENDER_PRIORITY_LOW,
)


class PreRenderEstimatePayload(BaseModel):
    """Inputs for POST /pre-render/estimate.

    Mirrors what ``ConfirmRenderGroupPayload`` carries minus the parts
    that only matter at submit time (tier — preview always runs every
    implemented tier; scheduling — orchestration concern).
    """

    analysis_snapshot: dict[str, Any] | None = None
    render_overrides: dict[str, Any] | None = None
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int | None = None
    machine_ids: list[str] | None = None
    file_size_bytes: int | None = None
    priority: int = Field(
        default=RENDER_PRIORITY_DEFAULT,
        ge=RENDER_PRIORITY_LOW,
        le=RENDER_PRIORITY_HIGH,
    )
