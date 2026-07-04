"""Admin dashboard routes — read-only aggregates for /home.

Every route is gated by ``require_admin`` (403 for non-admins).  All the
heavy lifting lives in ``AdminTelemetryService``; this router is pure
plumbing, mirroring the ``admin_config`` init pattern.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from serverV2.api.dependencies import require_admin
from serverV2.services.admin import AdminTelemetryService
from serverV2.services.admin.cost import CostAccountingService

router = APIRouter(prefix="/admin", tags=["admin_dashboard"])

_service: AdminTelemetryService | None = None
_cost: CostAccountingService | None = None
_gcp = None


def init(
    service: AdminTelemetryService,
    cost_service: CostAccountingService | None = None,
    gcp_metrics=None,
) -> None:
    global _service, _cost, _gcp
    _service = service
    _cost = cost_service
    _gcp = gcp_metrics


def _svc() -> AdminTelemetryService:
    if _service is None:
        raise HTTPException(500, "AdminTelemetryService not initialized")
    return _service


def _cost_svc() -> CostAccountingService:
    if _cost is None:
        raise HTTPException(500, "CostAccountingService not initialized")
    return _cost


@router.get("/overview")
def overview(_admin: dict = Depends(require_admin)):
    return _svc().overview()


@router.get("/daemons/{name}")
def daemon_detail(name: str, _admin: dict = Depends(require_admin)):
    """One daemon's health + per-tick history + activity/metric series."""
    return _svc().daemon_detail(name)


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
    return {"instances": _svc().serverless_instances(), "served_by": _svc().instance_id}


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
    """In-memory command stream + counters (ZERO Redis ops) — safe to poll
    live.  The fleet-wide INFO is a separate, slow endpoint (below)."""
    return _svc().redis_activity(recent_limit=limit)


@router.get("/redis/server")
def redis_server(_admin: dict = Depends(require_admin)):
    """Fleet-wide Redis INFO (real Redis reads) — polled slowly so watching the
    panel doesn't inflate the quota it reports."""
    return _svc().redis_server_info()


# -- cost accounting -------------------------------------------------------

@router.get("/costs/overview")
def costs_overview(_admin: dict = Depends(require_admin)):
    """Every paid dependency's cost, summarized (month-to-date + live burn)."""
    return _cost_svc().overview()


@router.get("/costs/source/{name}")
def costs_source(name: str, _admin: dict = Depends(require_admin)):
    """One cost source's detail (usage, series, breakdown) for the drill-down."""
    result = _cost_svc().source(name)
    if result is None:
        raise HTTPException(404, f"Unknown cost source: {name}")
    return result


@router.get("/gcp/metrics")
def gcp_metrics(days: int = 30, _admin: dict = Depends(require_admin)):
    """Cloud Run usage + cost per service (dev/staging/prod) from Cloud
    Monitoring.  Returns ``{available: false, note}`` until the runtime SA has
    ``roles/monitoring.viewer``.  Slow-polled by the UI."""
    if _gcp is None:
        return {"available": False, "note": "GCP metrics service not initialized."}
    return _gcp.metrics(days=days)


@router.post("/redis/reset")
def redis_activity_reset(_admin: dict = Depends(require_admin)):
    return _svc().redis_activity_reset()
