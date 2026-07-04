"""jobs_logger router -- HTTP endpoints for the worker-log capture flow.

Three endpoints, each with its own auth:

  POST /internal/jobs/{job_id}/log-append?exp=X&sig=Y
      Called by the worker's log_streamer subprocess every 10 s.  Auth
      is a HMAC-SHA256 signature over ``(job_id, exp)`` embedded in the
      query string; ``JobsLoggerService.verify_upload_signature`` is the
      arbiter.  Body is the gzipped delta bytes; ``X-Chunk-Offset`` is
      the offset the bytes start at; the first request per job additionally
      carries ``X-Env / X-Group-Id / X-Attempt / X-Chunk-Index / X-Fleet /
      X-Machine-Id`` so the backend can populate the meta hash.

  POST /internal/write-logs-to-r2
      Called by the backup_monitor Cloud Run Job on its 60 s tick.
      Auth is the existing ``X-Orphan-Secret`` shared with the other
      internal endpoints.  Delegates to
      ``JobsLoggerService.write_logs_to_r2()``, which scans Redis and
      moves every ready log into R2.

  GET /jobs/{job_id}/log
      Public (user-auth required).  Serves the log content directly
      when it's still in Redis, or 302 redirects to a presigned R2 URL
      when it's been archived.  Frontend instance cards consume this
      endpoint; they don't care where the storage lives.

The router itself is thin: signature check + delegate.  All schema
knowledge lives in ``JobsLoggerService`` and its collaborators.
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import RedirectResponse

from serverV2.api.dependencies import get_current_user
from serverV2.repositories.job_repository import JobRepository
from serverV2.services.jobs.jobs_logger.jobs_logger_service import (
    JobsLoggerService,
)

router = APIRouter(tags=["jobs_logger"])

log = logging.getLogger(__name__)


_service: JobsLoggerService | None = None
_orphan_secret: str = ""
_job_repo: JobRepository | None = None


def init(
    service: JobsLoggerService,
    *,
    orphan_secret: str,
    job_repo: JobRepository,
) -> None:
    global _service, _orphan_secret, _job_repo
    _service = service
    _orphan_secret = orphan_secret
    _job_repo = job_repo


def _require_service() -> JobsLoggerService:
    if _service is None:
        raise HTTPException(503, "jobs_logger not configured")
    return _service


def _require_job_repo() -> JobRepository:
    if _job_repo is None:
        raise HTTPException(503, "jobs_logger not configured")
    return _job_repo


# ------------------------------------------------------------
# Worker -> backend: append a gzipped chunk
# ------------------------------------------------------------

@router.post("/internal/jobs/{job_id}/log-append", status_code=204)
async def log_append(
    job_id: str,
    request: Request,
    exp: int = Query(...),
    sig: str = Query(...),
    x_chunk_offset: int = Header(..., alias="X-Chunk-Offset"),
    x_env: str | None = Header(default=None, alias="X-Env"),
    x_group_id: str | None = Header(default=None, alias="X-Group-Id"),
    x_attempt: str | None = Header(default=None, alias="X-Attempt"),
    x_chunk_index: str | None = Header(default=None, alias="X-Chunk-Index"),
    x_fleet: str | None = Header(default=None, alias="X-Fleet"),
    x_machine_id: str | None = Header(default=None, alias="X-Machine-Id"),
) -> Response:
    svc = _require_service()
    if not svc.verify_upload_signature(job_id, exp, sig):
        raise HTTPException(401, "Invalid signature")

    body = await request.body()
    if not body:
        return Response(status_code=204)

    # First chunk carries the identity headers so the backend can
    # populate the Redis meta hash.  Subsequent chunks only carry the
    # offset -- the mirror already knows the identity.
    first_ctx: dict | None = None
    if x_group_id and x_env:
        first_ctx = {
            "attempt":     x_attempt or "",
            "chunk_index": x_chunk_index or "",
            "fleet":       x_fleet or "",
            "machine_id":  x_machine_id or "",
        }

    env = x_env or ""
    group_id = x_group_id or ""
    if not env or not group_id:
        # Non-first chunk -- the mirror path builder still needs env +
        # group_id to route the RPUSH.  Pull them from the meta hash if
        # we can (the first-chunk POST that populated them is already
        # authenticated by the same signed URL).
        # Fail closed if we can't route -- worker will keep POSTing.
        raise HTTPException(
            400,
            "Missing X-Env / X-Group-Id; first-chunk headers required",
        )

    svc.append(
        env=env,
        group_id=group_id,
        job_id=job_id,
        offset=x_chunk_offset,
        gzipped=body,
        first_ctx=first_ctx,
    )
    return Response(status_code=204)


# ------------------------------------------------------------
# backup_monitor -> backend: flush ready Redis logs to R2
# ------------------------------------------------------------

@router.post("/internal/write-logs-to-r2")
def write_logs_to_r2(
    x_orphan_secret: str | None = Header(default=None, alias="X-Orphan-Secret"),
) -> dict:
    if not _orphan_secret:
        raise HTTPException(503, "Internal endpoint not configured")
    if x_orphan_secret != _orphan_secret:
        raise HTTPException(401, "Invalid X-Orphan-Secret")
    svc = _require_service()
    written = svc.write_logs_to_r2()
    return {"written": written}


# ------------------------------------------------------------
# Frontend -> backend: read a job's log
# ------------------------------------------------------------

@router.get("/jobs/{job_id}/log")
def get_job_log(
    job_id: str,
    _user: dict = Depends(get_current_user),
) -> Response:
    svc = _require_service()
    repo = _require_job_repo()

    row = repo.get_raw_by_id(job_id)
    if not row:
        raise HTTPException(404, "Job not found")

    group_id = str(row.get("group_id") or "")
    env = _resolve_env_for_read(row)

    live = svc.read_from_redis(env, group_id, job_id) if group_id else None
    if live is not None:
        return Response(content=live, media_type="text/plain; charset=utf-8")

    key = row.get("worker_log_url")
    if not key:
        raise HTTPException(404, "No log available for this job")

    return RedirectResponse(
        url=svc.presign_r2_url(str(key)),
        status_code=302,
    )


def _resolve_env_for_read(_row: dict) -> str:
    """Redis keys are namespaced by env; the deployment env is a
    process-level fact.  For the read path we just use ``REDIS_KEY_PREFIX``
    stripped of the trailing colon, so ``dev:`` -> ``dev``.  If nothing
    is set (prod), fall back to ``prod``."""
    import os
    prefix = os.environ.get("REDIS_KEY_PREFIX", "").strip().rstrip(":")
    return prefix or "prod"
