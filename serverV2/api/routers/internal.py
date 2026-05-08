"""Internal routes — called only by other backend services, never the
public.  Authenticated via shared secret in the ``X-Orphan-Secret``
header.

Three endpoints today, all consumed by the ``backup_monitor`` Cloud
Run Job:

  * ``POST /internal/orphan/{job_id}`` -- heartbeat-dead worker; the
    orchestrator routes it through ``CallbackRouter`` as a failure.

  * ``POST /internal/ghost-instance/vast/{vast_id}`` -- a Vast.ai
    instance the backup monitor classified as a ghost (terminal local
    row, or no local row + age > grace).  The orchestrator
    defensively re-checks the local row's status; if the row is
    somehow active again, the call is a logged no-op.  Otherwise the
    instance is destroyed via ``VastClient.instances.destroy`` (the
    method is idempotent and 404-tolerant).

  * ``POST /internal/ghost-instance/modal/{call_id}`` -- a Modal
    function call our DB believes terminal.  Same defensive re-check
    against ``modal_function_call_id``; if the row is active, no-op;
    otherwise ``ModalClient.cancel_job(call_id)`` (idempotent).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from serverV2.callbacks.router import CallbackRouter
from serverV2.core.enums import CallbackOutcome
from serverV2.fleets.modal.client import ModalClient
from serverV2.fleets.vast.client import VastClient
from serverV2.repositories.job_repository import JobRepository

router = APIRouter(tags=["internal"])

log = logging.getLogger(__name__)

_callback_router: CallbackRouter | None = None
_orphan_secret: str = ""
_vast_client: VastClient | None = None
_modal_client: ModalClient | None = None
_job_repo: JobRepository | None = None

_ACTIVE_STATUSES = frozenset({"pending", "running"})


def init(
    callback_router: CallbackRouter,
    orphan_secret: str,
    *,
    vast_client: VastClient,
    modal_client: ModalClient,
    job_repo: JobRepository,
) -> None:
    global _callback_router, _orphan_secret
    global _vast_client, _modal_client, _job_repo
    _callback_router = callback_router
    _orphan_secret = orphan_secret
    _vast_client = vast_client
    _modal_client = modal_client
    _job_repo = job_repo


def _check_secret(provided: str | None) -> None:
    if not _orphan_secret:
        raise HTTPException(503, "Internal endpoint not configured")
    if provided != _orphan_secret:
        raise HTTPException(401, "Invalid X-Orphan-Secret")


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
    _check_secret(x_orphan_secret)
    if _callback_router is None:
        raise HTTPException(500, "CallbackRouter not initialized")

    error = str(payload.get("error") or "Backup monitor: orphan detected")
    log.info("Orphan reported via backup monitor for job %s: %s", job_id, error)
    _callback_router.route(
        job_id=job_id, outcome=CallbackOutcome.FAILURE, error=error,
    )
    return {"job_id": job_id, "accepted": True}


@router.post("/internal/ghost-instance/vast/{vast_id}")
def report_vast_ghost(
    vast_id: int,
    payload: dict[str, Any],
    x_orphan_secret: str | None = Header(default=None, alias="X-Orphan-Secret"),
) -> dict[str, Any]:
    """Destroy a Vast.ai instance the backup monitor flagged as a
    ghost.  Defensive re-check against the live ``jobs`` row prevents
    racing a fresh dispatch that wrote ``vast_job_id`` after the
    backup monitor's snapshot was taken.
    """
    _check_secret(x_orphan_secret)
    if _vast_client is None or _job_repo is None:
        raise HTTPException(500, "Vast client / job repo not initialized")

    reason = str(payload.get("reason") or "unspecified")
    row = _job_repo.get_raw_by_vast_id(vast_id)
    if row is not None and str(row.get("status") or "") in _ACTIVE_STATUSES:
        log.info(
            "Ghost report for Vast %s skipped: local job %s is active "
            "(status=%s, reason=%s)",
            vast_id, row.get("id"), row.get("status"), reason,
        )
        return {"vast_id": vast_id, "destroyed": False, "skipped": "active"}

    log.info(
        "Destroying Vast.ai instance %s on backup-monitor ghost report (reason=%s)",
        vast_id, reason,
    )
    _vast_client.instances.destroy(vast_id)
    return {"vast_id": vast_id, "destroyed": True}


@router.post("/internal/ghost-instance/modal/{call_id}")
def report_modal_ghost(
    call_id: str,
    payload: dict[str, Any],
    x_orphan_secret: str | None = Header(default=None, alias="X-Orphan-Secret"),
) -> dict[str, Any]:
    """Cancel a Modal function call that our DB shows as terminal but
    that we may not have successfully cancelled.  Idempotent on the
    Modal side; safe to retry every backup-monitor tick.
    """
    _check_secret(x_orphan_secret)
    if _modal_client is None or _job_repo is None:
        raise HTTPException(500, "Modal client / job repo not initialized")

    reason = str(payload.get("reason") or "unspecified")
    row = _job_repo.get_raw_by_modal_function_call_id(call_id)
    if row is not None and str(row.get("status") or "") in _ACTIVE_STATUSES:
        log.info(
            "Ghost report for Modal call %s skipped: local job %s is active "
            "(status=%s, reason=%s)",
            call_id, row.get("id"), row.get("status"), reason,
        )
        return {"call_id": call_id, "cancelled": False, "skipped": "active"}

    log.info(
        "Cancelling Modal call %s on backup-monitor ghost report (reason=%s)",
        call_id, reason,
    )
    _modal_client.cancel_job(call_id)
    return {"call_id": call_id, "cancelled": True}
