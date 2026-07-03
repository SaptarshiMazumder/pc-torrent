"""Job routes — worker callbacks (status, progress, heartbeat), outputs, download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from serverV2.api.dependencies import get_current_user
from serverV2.api.schemas.job import (
    JobHeartbeatPayload,
    UpdateJobProgressPayload,
    UpdateJobStatusPayload,
    UpdateJobTelemetryPayload,
)
from serverV2.callbacks.router import CallbackRouter
from serverV2.core.enums import CallbackOutcome
from serverV2.orchestrator.lifecycle import (
    RETRY_REASON_HTTP_STATUS,
    ManualRetryError,
)
from serverV2.orchestrator.orchestrator import RenderOrchestrator
from serverV2.services.jobs.service import (
    JobService,
    JobServiceError,
    JobTerminalError,
)

router = APIRouter(tags=["jobs"])

_svc: JobService | None = None
_orchestrator: RenderOrchestrator | None = None
_callback_router: CallbackRouter | None = None
_download_stats = None


def init(
    service: JobService,
    *,
    orchestrator: RenderOrchestrator,
    callback_router: CallbackRouter,
    download_stats=None,
) -> None:
    global _svc, _orchestrator, _callback_router, _download_stats
    _svc = service
    _orchestrator = orchestrator
    _callback_router = callback_router
    _download_stats = download_stats


def _get() -> JobService:
    if _svc is None:
        raise HTTPException(500, "JobService not initialized")
    return _svc


def _get_orchestrator() -> RenderOrchestrator:
    if _orchestrator is None:
        raise HTTPException(500, "Orchestrator not initialized")
    return _orchestrator


def _get_callback_router() -> CallbackRouter:
    if _callback_router is None:
        raise HTTPException(500, "CallbackRouter not initialized")
    return _callback_router


# ---- status callbacks ----

def _terminal_response(exc: JobTerminalError) -> JSONResponse:
    """410 Gone with structured body so worker clients can detect a
    terminal job and exit cleanly instead of retrying the call."""
    return JSONResponse(status_code=410, content=exc.to_body())


@router.put("/jobs/{job_id}/status")
def update_status(
    job_id: str,
    payload: UpdateJobStatusPayload,
):
    """Worker self-reports a status transition.  Two cases reach here:

    * ``running``  — first signal that the worker has the chunk and is
      rendering.  Stamps ``started_at`` if not yet stamped.
    * ``failed``   — worker hit a fatal error.  Routes through the
      orchestrator failure pipeline (retry / reconcile).

    ``done`` is no longer a worker self-report on any fleet -- chunk
    completion is detected by the fleet singletons via
    ``JobCounts.is_complete``.  Old agents may still send it; we
    accept it (the service still writes status='done') but no
    success-chain firing here -- the singleton's next tick catches
    the completion via the data and routes through ``on_success``.
    """
    try:
        result = _get().update_status(job_id, payload.status, payload.error)
        if payload.output_files:
            # Legacy status+outputs path -- these agents don't tag the
            # primary render, so is_primary defaults False (the preview
            # selector falls back to naming/latest for them).
            _get().register_outputs(
                job_id, [(f, False) for f in payload.output_files],
            )
        return result
    except JobTerminalError as e:
        # Workers MUST be allowed to update terminal status -- but the
        # service catches "already terminal" inside update_status and
        # returns success=false instead of raising.  This branch
        # exists for defensive symmetry and for any future call site
        # that uses _assert_not_terminal here.
        return _terminal_response(e)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/jobs/{job_id}/allowed-stall-times")
def get_allowed_stall_times(job_id: str):
    """Return the resolved kill-time deadlines stamped on the job at
    dispatch.  UI fetches once per active-card mount, caches in memory,
    and computes "remaining" client-side every second.  404 if the job
    doesn't exist or pre-dates the column."""
    result = _get().get_allowed_stall_times(job_id)
    if result is None:
        raise HTTPException(404, "No allowed_stall_times for this job")
    return result


@router.put("/jobs/{job_id}/progress")
def update_progress(job_id: str, payload: UpdateJobProgressPayload):
    try:
        return _get().update_progress(
            job_id, payload.rendered_frames, payload.total_frames or 0,
        )
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/jobs/{job_id}/telemetry")
def update_telemetry(job_id: str, payload: UpdateJobTelemetryPayload):
    """Phase-11 data-richness intake.  Pure storage path -- writes the
    worker payload to jobs.worker_telemetry_json so the orchestrator
    can attach it to the render_telemetry row at completion time.

    Swallows JobServiceError into a 200 -- a failed telemetry write
    should never propagate back to the worker (it's fire-and-forget on
    the worker side; making it strict here invites useless retries).
    """
    try:
        _get().update_telemetry(job_id, payload.telemetry or {})
    except JobServiceError:
        # Already logged inside the service; keep responding 200 so the
        # worker can move on without retrying observational data.
        pass
    return {"ok": True}


@router.put("/jobs/{job_id}/heartbeat")
def heartbeat(job_id: str, payload: JobHeartbeatPayload | None = None):
    try:
        return _get().heartbeat(
            job_id,
            phase=payload.phase if payload else None,
            cpu_percent=payload.cpu_percent if payload else None,
            rss_bytes=payload.rss_bytes if payload else None,
            bytes_progressed=payload.bytes_progressed if payload else None,
            total_bytes=payload.total_bytes if payload else None,
        )
    except JobTerminalError as e:
        return _terminal_response(e)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.post("/jobs/{job_id}/agent-failure")
def agent_failure(job_id: str, body: dict):
    """Community-agent monitor-equivalent path.  The agent is its own
    monitor — when its local render fails, it tells the orchestrator
    here so the standard retry / drain / reconcile flow fires.
    """
    error = str(body.get("error") or "Agent reported failure")
    _get_callback_router().route(
        job_id=job_id, outcome=CallbackOutcome.FAILURE, error=error,
    )
    return {"job_id": job_id, "accepted": True}


@router.post("/jobs/{job_id}/cancel")
def cancel_one_job(job_id: str):
    """Per-instance Cancel button (B2).  Tears down ONE job (the
    chunk the user clicked Cancel on) without touching the rest of
    the render group.  Cancel chain (mark cancelled, stop monitor,
    release ledger, RPC the provider) runs synchronously — the
    provider call can take up to ~30s.
    """
    _get_orchestrator().cancel_one_job(job_id)
    return {"job_id": job_id, "accepted": True}


@router.get("/jobs/{job_id}/cancel-status")
def cancel_status(job_id: str):
    """Long-running workers (community renderer) poll this every ~30s to
    detect server-side cancellation.  Returns true when the orchestrator
    has marked the job cancelled or failed."""
    try:
        return _get().get_cancel_status(job_id)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.put("/jobs/{job_id}/worker-start")
def worker_start(job_id: str):
    """Serverless worker's first action: claim the start.  Returns 200 to
    the first caller and 409 to any duplicate — Modal's silent re-queue of
    the same FunctionCall input is defeated by this guard.
    """
    claimed = _get().try_claim_worker_start(job_id)
    if not claimed:
        raise HTTPException(
            409, f"Job {job_id} already started by another container",
        )
    return {"first_start": True}


# ---- reads ----

@router.get("/jobs/next-for-machine/{machine_id}")
def next_for_machine(machine_id: str):
    result = _get().next_for_machine(machine_id)
    if result is None:
        return {"job": None}
    return {"job": result}


# ---- output management ----

@router.post("/jobs/{job_id}/request-upload-urls")
def request_upload_urls(job_id: str, body: dict):
    filenames = body.get("filenames", []) or []
    try:
        urls = _get().request_upload_urls(job_id, filenames)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)
    return {"urls": urls}


@router.get("/jobs/{job_id}/outputs")
def get_outputs(job_id: str):
    try:
        return _get().get_outputs(job_id)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/jobs/{job_id}/output/{filename}")
def download_output(job_id: str, filename: str):
    try:
        url = _get().get_output_download_url(job_id, filename)
        return RedirectResponse(url)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


@router.get("/jobs/{job_id}/output/{filename}/preview")
def preview_output(job_id: str, filename: str):
    try:
        payload, media_type = _get().get_output_preview(job_id, filename)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get("/jobs/{job_id}/download-zip")
def download_zip(job_id: str):
    try:
        buf = _get().download_all_as_zip(job_id)
        if _download_stats is not None:
            _download_stats.record("job_zip")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="job-{job_id[:8]}-output.zip"'},
        )
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


def _parse_output_files(body: dict) -> list[tuple[str, bool]]:
    """Normalize the register-outputs payload to ``(filename, is_primary)``.

    New worker shape: ``{"files": [{"filename": str, "is_primary": bool}]}``
    (the worker tags the main render vs File Output passes).  Legacy shape:
    ``{"filenames": [str]}`` -> ``is_primary`` defaults False for workers
    not yet redeployed.
    """
    files = body.get("files")
    if isinstance(files, list) and files and isinstance(files[0], dict):
        return [
            (f["filename"], bool(f.get("is_primary")))
            for f in files
            if isinstance(f, dict) and f.get("filename")
        ]
    return [(f, False) for f in (body.get("filenames") or []) if f]


@router.post("/jobs/{job_id}/register-outputs")
def register_outputs(job_id: str, body: dict):
    """Worker tells us a file is in R2.  We record it and return.
    Chunk completion is detected by the fleet singleton on its next
    tick (~10s), which fires the success chain through the standard
    callback router path.  Keeping this route as a pure write avoids
    the layering smudge of HTTP-time business logic.
    """
    files = _parse_output_files(body)
    if not files:
        raise HTTPException(400, "No filenames provided")
    try:
        return _get().register_outputs(job_id, files)
    except JobServiceError as e:
        raise HTTPException(e.status, e.message)


# ---- user-triggered manual retry ----

@router.post("/jobs/{job_id}/retry")
def retry_chunk(job_id: str):
    """Re-dispatch a stuck chunk's missing frames with a fresh attempt
    counter.  Refused if the chunk's auto-retries haven't been exhausted,
    if a sibling is still active, if the group is cancelled, or if no
    frames are actually missing."""
    try:
        return _get_orchestrator().retry_chunk_manually(job_id)
    except ManualRetryError as e:
        status = RETRY_REASON_HTTP_STATUS.get(e.reason, 400)
        raise HTTPException(status, e.reason)
