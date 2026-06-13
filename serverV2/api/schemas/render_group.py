from typing import Any
from pydantic import BaseModel, Field

from serverV2.core.value_objects import (
    RENDER_PRIORITY_DEFAULT,
    RENDER_PRIORITY_HIGH,
    RENDER_PRIORITY_LOW,
)


class CreateRenderGroupPayload(BaseModel):
    machine_ids: list[str] | None = None
    filename: str | None = None
    file_size_bytes: int | None = None
    source_asset_id: str | None = None


class ConfirmRenderGroupPayload(BaseModel):
    machine_ids: list[str] | None = None
    frame_start: int | None = None
    frame_end: int | None = None
    frame_step: int | None = None
    render_overrides: dict[str, Any] | None = None
    scheduling: dict[str, Any] | None = None
    analysis_snapshot: dict[str, Any] | None = None
    # Phase 8 — user-selected allocation tier ("economy" | "standard").
    # Server normalises and defaults to "standard" if missing/unknown.
    tier: str | None = None
    # Ordering key for the pending + dispatch queues.  0=LOW, 1=NORMAL,
    # 2=HIGH.  Out-of-range submissions get a 422 from Pydantic; missing
    # value defaults to NORMAL (queue position unchanged from today's
    # default behaviour).
    priority: int = Field(
        default=RENDER_PRIORITY_DEFAULT,
        ge=RENDER_PRIORITY_LOW,
        le=RENDER_PRIORITY_HIGH,
    )


class UpdateInputFilePayload(BaseModel):
    display_name: str
