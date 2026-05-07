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

from serverV2.callbacks.router import CallbackRouter
from serverV2.core.enums import CallbackOutcome

router = APIRouter(tags=["internal"])

log = logging.getLogger(__name__)

_callback_router: CallbackRouter | None = None
_orphan_secret: str = ""


def init(callback_router: CallbackRouter, orphan_secret: str) -> None:
    global _callback_router, _orphan_secret
    _callback_router = callback_router
    _orphan_secret = orphan_secret


@router.post("/internal/orphan/{job_id}")
def report_orphan(
    job_id: str,
    payload: dict[str, Any],
    x_orphan_secret: str | None = Header(default=None, alias="X-Orphan-Secret"),
) -> dict[str, Any]:
    """Backup monitor reports a job whose heartbeat has gone dead.
    The CallbackRouter chain runs synchronously — cron tolerates
    multi-second waits.
    """
    if not _orphan_secret:
        # Endpoint disabled when no secret is configured.
        raise HTTPException(503, "Internal orphan endpoint not configured")
    if x_orphan_secret != _orphan_secret:
        raise HTTPException(401, "Invalid X-Orphan-Secret")
    if _callback_router is None:
        raise HTTPException(500, "CallbackRouter not initialized")

    error = str(payload.get("error") or "Backup monitor: orphan detected")
    log.info("Orphan reported via backup monitor for job %s: %s", job_id, error)
    _callback_router.route(
        job_id=job_id, outcome=CallbackOutcome.FAILURE, error=error,
    )
    return {"job_id": job_id, "accepted": True}
