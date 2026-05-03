"""Resolves the blend file download URL per fleet type."""

from __future__ import annotations

from serverV2.config import AppConfig


class AllocationBlendUrlResolver:

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config

    def resolve(self, fleet: str, group_id: str, input_filename: str) -> str:
        if fleet == "modal_serverless":
            base = self._cfg.modal.public_backend_url
        else:
            base = self._cfg.vast.public_backend_url
        return f"{base}/render-groups/{group_id}/input/{input_filename}"
