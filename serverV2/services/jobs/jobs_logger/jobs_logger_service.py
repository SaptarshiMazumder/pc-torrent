"""JobsLoggerService -- orchestrator for per-worker log capture.

Owns the DECISIONS, not the I/O:
  * When to write meta vs just a chunk (first tick vs subsequent).
  * Whether a Redis-side log is ready to move to R2 (terminal job or
    stale worker).
  * When to delete the Redis keys (immediately after R2 upload, or
    after R2 upload we consider unnecessary).
  * How the signed URL is constructed and verified.

Zero direct Redis / R2 / Postgres calls in this file -- everything is
delegated to the mirror / R2 repository / job repository.  Change the
Redis schema, R2 layout, or Postgres column here and only the affected
collaborator changes.

Signed URL:
  * ``sign_upload_url(job_id) -> URL`` -- stamped at dispatch by the
    fleet strategy so the worker never needs auth credentials.
  * URL shape: ``{public_url_base}/internal/jobs/{job_id}/log-append?
    exp={unix_ts}&sig={hex_hmac}``.
  * ``sig = HMAC_SHA256(secret, f"{job_id}|{exp}")``.  Verifier
    recomputes with the same secret and constant-time-compares.
  * 6 h TTL covers the longest expected render.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import logging
import time
from typing import Optional

from serverV2.repositories.job_repository import JobRepository
from serverV2.services.jobs.jobs_logger.jobs_logger_redis_mirror import (
    JobsLoggerRedisMirror,
)
from serverV2.services.jobs.jobs_logger.jobs_logger_r2_repository import (
    JobsLoggerR2Repository,
)

log = logging.getLogger(__name__)

_UPLOAD_URL_TTL_SEC = 6 * 3600
_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class JobsLoggerService:

    def __init__(
        self,
        *,
        mirror: JobsLoggerRedisMirror,
        r2_repo: JobsLoggerR2Repository,
        job_repo: JobRepository,
        hmac_secret: str,
        public_url_base: str,
    ) -> None:
        self._mirror = mirror
        self._r2_repo = r2_repo
        self._job_repo = job_repo
        self._hmac_secret = hmac_secret
        self._public_url_base = public_url_base.rstrip("/")

    # ------------------------------------------------------------
    # WRITE PATH -- called by router on every worker POST
    # ------------------------------------------------------------

    def append(
        self,
        *,
        env: str,
        group_id: str,
        job_id: str,
        offset: int,
        gzipped: bytes,
    ) -> None:
        self._mirror.append_chunk(env, group_id, job_id, offset, gzipped)

    # ------------------------------------------------------------
    # READ PATH -- Redis first, R2 fallback via presigned URL
    # ------------------------------------------------------------

    def read_from_redis(
        self, env: str, group_id: str, job_id: str,
    ) -> Optional[bytes]:
        """Return the assembled log bytes (decompressed) if the job's
        chunks are still in Redis; ``None`` if the key doesn't exist.
        A malformed chunk raises -- fail loud, we wrote it, we must
        be able to read it back."""
        chunks = self._mirror.read_chunks(env, group_id, job_id)
        if not chunks:
            return None
        chunks.sort(key=lambda t: t[0])
        return b"".join(gzip.decompress(gz) for _, gz in chunks)

    def presign_r2_url(self, key: str, expires_in: int = 3600) -> str:
        return self._r2_repo.presign_get(key, expires_in=expires_in)

    # ------------------------------------------------------------
    # WRITE-TO-R2 PATH -- called via internal endpoint by
    # backup_monitor's LogWriteTrigger on its 60 s tick
    # ------------------------------------------------------------

    def write_logs_to_r2(self) -> int:
        """Move every Redis-side log whose job is terminal (or whose
        worker has gone silent for >5 min) into R2.  Returns the number
        of logs written.

        Fail loud: any exception in ``_process_one`` propagates to the
        router (HTTP 500).  ``backup_monitor``'s ``LogWriteTrigger`` will
        log the non-200 response at WARNING, and the operator sees the
        failure immediately instead of a silent ``{"written": 0}``."""
        n = 0
        for env, group_id, job_id in self._mirror.scan_log_keys():
            if self._process_one(env, group_id, job_id):
                n += 1
        return n

    def _process_one(self, env: str, group_id: str, job_id: str) -> bool:
        row = self._job_repo.get_raw_by_id(job_id)
        if not row:
            # Chunks in Redis for a job we don't have a DB row for --
            # somebody deleted the row, or key drifted.  Drop the key
            # and move on.  Not defensive; this is a real "nothing to
            # archive against" state.
            log.info(
                "write_logs_to_r2: job %s not in DB; dropping Redis key",
                job_id,
            )
            self._mirror.delete(env, group_id, job_id)
            return False

        chunks = self._mirror.read_chunks(env, group_id, job_id)
        if not chunks:
            # Key exists but returned an empty list -- concurrent expire
            # or drain.  Real state, drop the key.
            self._mirror.delete(env, group_id, job_id)
            return False

        chunks.sort(key=lambda t: t[0])
        # gzip.decompress errors bubble up -- if the worker wrote it and
        # append_chunk stored it, we MUST be able to read it back.  A
        # decode failure here is a genuine bug we want to see.
        combined = b"".join(gzip.decompress(gz) for _, gz in chunks)
        recompressed = gzip.compress(combined)
        # R2 upload errors bubble up -- exactly the class of failure the
        # x-amz-tagging incident hid for hours behind a swallow.  Every
        # tick uploads the CURRENT state of Redis to R2, overwriting the
        # object.  A render in progress gets partial snapshots; a done
        # render gets a final complete snapshot on the tick after the
        # worker stops posting.  Idempotent (same Redis input -> same
        # R2 bytes), and any observer can fetch the log at any time
        # instead of only after the job hits a terminal status.
        r2_key = self._r2_repo.write_log(
            env=env,
            group_id=str(row.get("group_id") or group_id),
            chunk_index=int(row.get("chunk_index") or 0),
            attempt=int(row.get("attempt") or 0),
            job_id=job_id,
            gzipped=recompressed,
        )
        self._job_repo.stamp_worker_log_url(job_id, r2_key)

        # Only delete the Redis key once the job is terminal.  While the
        # render is still running the worker keeps appending, so keeping
        # Redis intact lets the live-tail read path serve fresh bytes;
        # each tick just overwrites the R2 object with the newer state.
        # After terminal, no more appends will come -- delete the key so
        # subsequent ticks skip this job cleanly instead of re-uploading
        # the same bytes until TTL expires.
        status = row.get("status") or ""
        if status in _TERMINAL_STATUSES:
            self._mirror.delete(env, group_id, job_id)
        return True

    # ------------------------------------------------------------
    # SIGNED URL -- HMAC over (job_id, exp)
    # ------------------------------------------------------------

    def sign_upload_url(self, job_id: str) -> str:
        exp = int(time.time()) + _UPLOAD_URL_TTL_SEC
        sig = self._sign(job_id, exp)
        return (
            f"{self._public_url_base}/internal/jobs/{job_id}/log-append"
            f"?exp={exp}&sig={sig}"
        )

    def verify_upload_signature(
        self, job_id: str, exp: int, sig: str,
    ) -> bool:
        if not self._hmac_secret:
            return False
        if exp < int(time.time()):
            return False
        expected = self._sign(job_id, exp)
        return hmac.compare_digest(expected, sig)

    def _sign(self, job_id: str, exp: int) -> str:
        msg = f"{job_id}|{exp}".encode("utf-8")
        return hmac.new(
            self._hmac_secret.encode("utf-8"),
            msg,
            hashlib.sha256,
        ).hexdigest()
