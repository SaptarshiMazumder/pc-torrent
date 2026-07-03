"""Admin dashboard routes — read-only aggregates for /home.

Every route is gated by ``require_admin`` (403 for non-admins).  All the
heavy lifting lives in ``AdminTelemetryService``; this router is pure
plumbing, mirroring the ``admin_config`` init pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import require_admin
from serverV2.services.admin import AdminTelemetryService

router = APIRouter(prefix="/admin", tags=["admin_dashboard"])

_service: AdminTelemetryService | None = None


def init(service: AdminTelemetryService) -> None:
    global _service
    _service = service


def _svc() -> AdminTelemetryService:
    if _service is None:
        raise HTTPException(500, "AdminTelemetryService not initialized")
    return _service


@router.get("/overview")
def overview(_admin: dict = Depends(require_admin)):
    return _svc().overview()


@router.get("/render-groups/active")
def active_render_groups(_admin: dict = Depends(require_admin)):
    return {"groups": _svc().active_render_groups()}


@router.get("/jobs/active")
def active_jobs(_admin: dict = Depends(require_admin)):
    return {"jobs": _svc().active_jobs()}


@router.get("/machines")
def machines(_admin: dict = Depends(require_admin)):
    return {"machines": _svc().machines()}


@router.get("/instances")
def serverless_instances(_admin: dict = Depends(require_admin)):
    return {"instances": _svc().serverless_instances()}


@router.get("/telemetry")
def render_telemetry(
    limit: int = 100,
    group_id: str | None = None,
    _admin: dict = Depends(require_admin),
):
    return {"telemetry": _svc().render_telemetry(limit=limit, group_id=group_id)}


@router.get("/users")
def users(_admin: dict = Depends(require_admin)):
    return {"users": _svc().users()}


@router.get("/users/{uid}")
def user_detail(uid: str, _admin: dict = Depends(require_admin)):
    return _svc().user_detail(uid)


@router.get("/costs/summary")
def costs_summary(days: int = 7, _admin: dict = Depends(require_admin)):
    return _svc().costs_summary(days=days)


@router.get("/failures/recent")
def failures_recent(limit: int = 50, _admin: dict = Depends(require_admin)):
    return {"failures": _svc().failures_recent(limit=limit)}


@router.get("/downloads")
def downloads(_admin: dict = Depends(require_admin)):
    return {"downloads": _svc().downloads()}


@router.get("/redis")
def redis_activity(limit: int = 200, _admin: dict = Depends(require_admin)):
    return _svc().redis_activity(recent_limit=limit)


@router.post("/redis/reset")
def redis_activity_reset(_admin: dict = Depends(require_admin)):
    return _svc().redis_activity_reset()
