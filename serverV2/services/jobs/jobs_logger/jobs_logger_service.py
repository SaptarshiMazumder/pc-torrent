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
_STALE_NO_CHUNK_SEC = 300
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
        first_ctx: Optional[dict],
    ) -> None:
        if first_ctx is not None:
            self._mirror.write_meta(env, group_id, job_id, first_ctx)
        self._mirror.append_chunk(env, group_id, job_id, offset, gzipped)
        self._mirror.touch_meta(env, group_id, job_id)

    # ------------------------------------------------------------
    # READ PATH -- Redis first, R2 fallback via presigned URL
    # ------------------------------------------------------------

    def read_from_redis(
        self, env: str, group_id: str, job_id: str,
    ) -> Optional[bytes]:
        """Return the assembled log bytes (decompressed) if the job's
        chunks are still in Redis; ``None`` otherwise."""
        chunks = self._mirror.read_chunks(env, group_id, job_id)
        if not chunks:
            return None
        chunks.sort(key=lambda t: t[0])
        parts: list[bytes] = []
        for _, gz in chunks:
            try:
                parts.append(gzip.decompress(gz))
            except OSError:
                continue
        return b"".join(parts) if parts else None

    def presign_r2_url(self, key: str, expires_in: int = 3600) -> str:
        return self._r2_repo.presign_get(key, expires_in=expires_in)

    # ------------------------------------------------------------
    # WRITE-TO-R2 PATH -- called via internal endpoint by
    # backup_monitor's LogWriteTrigger on its 60 s tick
    # ------------------------------------------------------------

    def write_logs_to_r2(self) -> int:
        """Move every Redis-side log whose job is terminal (or whose
        worker has gone silent for >5 min) into R2.  Returns the number
        of logs written."""
        n = 0
        for env, group_id, job_id in self._mirror.scan_log_keys():
            try:
                if self._process_one(env, group_id, job_id):
                    n += 1
            except Exception as exc:
                log.warning(
                    "write_logs_to_r2: unhandled error on %s: %s",
                    job_id, exc,
                )
        return n

    def _process_one(self, env: str, group_id: str, job_id: str) -> bool:
        meta = self._mirror.read_meta(env, group_id, job_id) or {}
        row = self._job_repo.get_raw_by_id(job_id)
        if not row:
            log.warning(
                "write_logs_to_r2: job %s not in DB; dropping Redis keys",
                job_id,
            )
            self._mirror.delete(env, group_id, job_id)
            return False

        status = row.get("status") or ""
        terminal = status in _TERMINAL_STATUSES
        stale = False
        try:
            last_seen = int(meta.get("last_seen_at", "0") or 0)
            stale = (int(time.time()) - last_seen) > _STALE_NO_CHUNK_SEC
        except (TypeError, ValueError):
            pass
        if not (terminal or stale):
            return False

        chunks = self._mirror.read_chunks(env, group_id, job_id)
        if not chunks:
            self._mirror.delete(env, group_id, job_id)
            return False

        chunks.sort(key=lambda t: t[0])
        decoded_parts: list[bytes] = []
        for _, gz in chunks:
            try:
                decoded_parts.append(gzip.decompress(gz))
            except OSError as exc:
                log.warning(
                    "write_logs_to_r2: chunk decompress failed for %s: %s",
                    job_id, exc,
                )
        if not decoded_parts:
            self._mirror.delete(env, group_id, job_id)
            return False

        combined = b"".join(decoded_parts)
        recompressed = gzip.compress(combined)
        try:
            r2_key = self._r2_repo.write_log(
                env=env,
                group_id=str(row.get("group_id") or group_id),
                chunk_index=int(row.get("chunk_index") or 0),
                attempt=int(row.get("attempt") or 0),
                job_id=job_id,
                gzipped=recompressed,
            )
        except Exception as exc:
            log.warning(
                "write_logs_to_r2: R2 upload failed for %s: %s",
                job_id, exc,
            )
            return False

        self._job_repo.stamp_worker_log_url(job_id, r2_key)
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
