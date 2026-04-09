from typing import Any
from pydantic import BaseModel


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


class UpdateInputFilePayload(BaseModel):
    display_name: str


class UpdateProfilePayload(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    billing_plan: str | None = None
