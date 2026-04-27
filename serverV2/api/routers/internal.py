"""Internal routes — called only by other backend services, never the
public.  Authenticated via shared secret in the ``X-Orphan-Secret``
header.

Currently exposes ``POST /internal/orphan/{job_id}`` for the
backup_monitor service to report jobs whose worker heartbeat has died
without a per-job monitor noticing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from serverV2.orchestrator.orchestrator import RenderOrchestrator

router = APIRouter(tags=["internal"])

log = logging.getLogger(__name__)

_orchestrator: RenderOrchestrator | None = None
_orphan_secret: str = ""


def init(orchestrator: RenderOrchestrator, orphan_secret: str) -> None:
    global _orchestrator, _orphan_secret
    _orchestrator = orchestrator
    _orphan_secret = orphan_secret


@router.post("/internal/orphan/{job_id}")
def report_orphan(
    job_id: str,
    payload: dict[str, Any],
    x_orphan_secret: str | None = Header(default=None, alias="X-Orphan-Secret"),
) -> dict[str, Any]:
    """Backup monitor reports a job whose heartbeat has gone dead.
    Routes through the orchestrator's standard failure path — exactly
    the same code an in-process monitor would have called.
    """
    if not _orphan_secret:
        # Endpoint disabled when no secret is configured.
        raise HTTPException(503, "Internal orphan endpoint not configured")
    if x_orphan_secret != _orphan_secret:
        raise HTTPException(401, "Invalid X-Orphan-Secret")
    if _orchestrator is None:
        raise HTTPException(500, "Orchestrator not initialized")

    error = str(payload.get("error") or "Backup monitor: orphan detected")
    log.info("Orphan reported via backup monitor for job %s: %s", job_id, error)
    _orchestrator.on_job_failed(job_id, error)
    return {"job_id": job_id, "accepted": True}
