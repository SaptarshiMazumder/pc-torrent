"""Admin config routes — read/write the Firestore config blob.

GET  /admin/config -> returns ``{config: <dict>}``.  Dict shape is
                      identical to bundled ``serverV2/config.json``.

PUT  /admin/config -> accepts ``{config: <dict>}``.  Facade validates
                      via ``RenderConfig.from_dict(d)`` before writing;
                      bad shape -> 400 with the validation error
                      message, never reaches Firestore.

No auth gate today (will gate admin-only later).  No write-side
validation rules beyond the implicit shape check at the facade.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from serverV2.fleets.fleet_exception import FleetException
from serverV2.orchestrator.allocation_client import AllocationClient

router = APIRouter(tags=["admin_config"])

_allocation_client: AllocationClient | None = None


def init(allocation_client: AllocationClient) -> None:
    global _allocation_client
    _allocation_client = allocation_client


def _get_client() -> AllocationClient:
    if _allocation_client is None:
        raise HTTPException(500, "AllocationClient not initialized")
    return _allocation_client


@router.get("/admin/config")
def get_admin_config():
    return {"config": _get_client().admin_get_config()}


@router.put("/admin/config")
def put_admin_config(body: dict = Body(...)):
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "Body must include a 'config' object")
    try:
        _get_client().admin_put_config(cfg)
    except FleetException as e:
        # RenderConfig.from_dict raises FleetException on bad shape.
        raise HTTPException(400, str(e))
    return {"success": True}
