"""AssetService — user input files CRUD + backfill."""

from __future__ import annotations

import logging
from typing import Any

from serverV2.infrastructure import storage

log = logging.getLogger(__name__)


class AssetServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class AssetService:

    def __init__(self, *, asset_repo) -> None:
        self._assets = asset_repo

    def list_assets(self, user_id: str) -> list[dict[str, Any]]:
        return self._assets.list_by_user(user_id)

    def get_asset(self, asset_id: str, user_id: str) -> dict[str, Any]:
        asset = self._assets.get_by_id(asset_id, user_id)
        if not asset:
            raise AssetServiceError(404, "Input file not found")
        return asset

    def rename_asset(self, asset_id: str, display_name: str, user_id: str) -> dict[str, Any]:
        asset = self._assets.get_by_id(asset_id, user_id)
        if not asset:
            raise AssetServiceError(404, "Input file not found")
        if not display_name or not display_name.strip():
            raise AssetServiceError(400, "display_name is required")
        self._assets.rename(asset_id, display_name.strip())
        return {"success": True, "asset_id": asset_id}

    def delete_asset(self, asset_id: str, user_id: str) -> dict[str, Any]:
        asset = self._assets.get_by_id(asset_id, user_id)
        if not asset:
            raise AssetServiceError(404, "Input file not found")
        r2_key = asset.get("r2_key", "")
        has_active = self._assets.has_active_references(r2_key, user_id)
        self._assets.delete(asset_id, user_id)
        if r2_key and not has_active:
            try:
                storage.delete_file(r2_key)
            except Exception as exc:
                log.warning("Failed to delete R2 file %s: %s", r2_key, exc)
        return {"success": True, "asset_id": asset_id}

    def backfill(self, user_id: str) -> dict[str, Any]:
        self._assets.backfill_from_groups(user_id)
        return {"success": True, "user_id": user_id}
