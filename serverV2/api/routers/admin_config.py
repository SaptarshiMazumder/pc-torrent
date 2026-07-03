"""Admin config routes — read/write the Firestore config blob.

GET  /admin/config -> returns ``{config: <dict>}``.  Dict shape is
                      identical to bundled ``serverV2/config.json``.

GET  /admin/config/cost-estimation -> returns ``{config: <dict>}`` for the
                      separate ``config/cost_estimation`` Firestore doc.

PUT  /admin/config -> accepts ``{config: <dict>}``.  Facade validates
                      via ``RenderConfig.from_dict(d)`` before writing;
                      bad shape -> 400 with the validation error
                      message, never reaches Firestore.

ALL routes are admin-gated via ``require_admin`` (403 for non-admins).
GET used to be open, but the config exposes fleet caps, pricing, and the
full allocation tuning surface -- infra detail that shouldn't be world-
readable.  The desktop only calls GET from its admin-only Configuration
view, so gating it doesn't affect non-admin clients.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Body, Depends, HTTPException

from serverV2.api.dependencies import require_admin
from serverV2.fleets.fleet_exception import FleetException
from serverV2.clients.allocation_client import AllocationClient

router = APIRouter(tags=["admin_config"])

_allocation_client: AllocationClient | None = None
_cost_estimation_repo = None


def init(allocation_client: AllocationClient, cost_estimation_repo=None) -> None:
    global _allocation_client, _cost_estimation_repo
    _allocation_client = allocation_client
    _cost_estimation_repo = cost_estimation_repo


def _get_client() -> AllocationClient:
    if _allocation_client is None:
        raise HTTPException(500, "AllocationClient not initialized")
    return _allocation_client


@router.get("/admin/config")
def get_admin_config(_admin: dict = Depends(require_admin)):
    return {"config": _get_client().admin_get_config()}


@router.get("/admin/config/cost-estimation")
def get_cost_estimation_config(_admin: dict = Depends(require_admin)):
    if _cost_estimation_repo is None:
        raise HTTPException(500, "Cost estimation config repo not initialized")
    return {"config": asdict(_cost_estimation_repo.get())}


@router.put("/admin/config")
def put_admin_config(
    body: dict = Body(...),
    _admin: dict = Depends(require_admin),
):
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "Body must include a 'config' object")
    try:
        _get_client().admin_put_config(cfg)
    except FleetException as e:
        # RenderConfig.from_dict raises FleetException on bad shape.
        raise HTTPException(400, str(e))
    return {"success": True}
