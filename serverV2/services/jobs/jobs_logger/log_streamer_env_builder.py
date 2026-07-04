"""LogStreamerEnvBuilder -- builds the PCR_* env dict every worker's
log_streamer subprocess needs.

Single source of truth for the wire contract with
``worker_core/log_streamer.py``.  The dispatch orchestration layer
calls ``build(...)`` once per job and stamps the returned dict onto
``DispatchContext.log_streamer_env``.  Fleet strategies never call
this class directly -- they read the pre-built dict off the context
and forward it to their fleet client.

The env dict produced (str -> str):

    PCR_LOG_ENDPOINT   HMAC-signed URL the worker POSTs chunks to
                       (job_id is signed into the URL path)
    PCR_ENV            deployment env (dev / staging / prod)
    PCR_GROUP_ID       render_group_id (needed for Redis key routing)

Why a class, not a free function: holds a JobsLoggerService reference
and the deployment env string, so callers don't have to thread them
through every call site.
"""

from __future__ import annotations

from serverV2.services.jobs.jobs_logger.jobs_logger_service import (
    JobsLoggerService,
)


class LogStreamerEnvBuilder:

    def __init__(
        self,
        *,
        jobs_logger_service: JobsLoggerService,
        env: str,
    ) -> None:
        self._jobs_logger_service = jobs_logger_service
        self._env = env

    def build(
        self,
        *,
        job_id: str,
        group_id: str,
    ) -> dict[str, str]:
        return {
            "PCR_LOG_ENDPOINT":
                self._jobs_logger_service.sign_upload_url(job_id),
            "PCR_ENV":         self._env,
            "PCR_GROUP_ID":    group_id,
        }
