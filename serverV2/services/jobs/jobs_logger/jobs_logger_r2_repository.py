"""JobsLoggerR2Repository -- pure R2 I/O for worker log objects.

Owns the R2 path builder -- the ONLY place in the codebase that decides
where a worker's log lives *within its bucket*.  The bucket itself is
injected at construction (composition root decides the deploy-time
target: ``pc-rent-logs-<env>`` by convention).  Zero business logic
beyond that: no Redis, no Postgres, no "when to write" decisions.

Layout (per attempt = one object, never overwritten):

    {env}/
      {group_id}/
        chunk_{chunk_index}/
          attempt_{attempt}__{job_id}.log.gz

Each object carries R2 tags used by lifecycle rules:

    created_date=YYYY-MM-DD   -- for age-based deletion
    env={env}                 -- redundant with bucket, but sanity-check
                                 if a misconfigured deploy ever points
                                 dev's client at prod's bucket

Deployment TODO (per env, one-time in Cloudflare R2 dashboard):
    Add a lifecycle rule on the ``pc-rent-logs-<env>`` bucket:
    "delete objects older than 30 days" (adjust as needed).  Alpha
    debugging rarely needs older logs, and storage grows unbounded
    without a rule.

We keep the object key in ``jobs.worker_log_url`` rather than a
presigned URL because presigned URLs expire.  The frontend read path
(``GET /jobs/{id}/log``) fetches the key and presigns on demand.
"""

from __future__ import annotations

import logging
from datetime import date

from serverV2.infrastructure.storage.client import get_s3_client

log = logging.getLogger(__name__)


class JobsLoggerR2Repository:

    def __init__(self, *, bucket: str) -> None:
        self._bucket = bucket

    def write_log(
        self,
        *,
        env: str,
        group_id: str,
        chunk_index: int,
        attempt: int,
        job_id: str,
        gzipped: bytes,
    ) -> str:
        """Upload the gzipped log bytes and return the R2 object key
        (relative -- callers presign / route as needed)."""
        client = get_s3_client()
        key = self._build_path(env, group_id, chunk_index, attempt, job_id)
        client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=gzipped,
            ContentType="application/gzip",
            ContentEncoding="gzip",
            Tagging=f"created_date={date.today().isoformat()}&env={env}",
        )
        return key

    def read_log(self, key: str) -> bytes:
        """Fetch a full log object.  Used by the read endpoint's R2
        fallback and future admin bulk-export."""
        client = get_s3_client()
        response = client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def presign_get(self, key: str, expires_in: int = 3600) -> str:
        """Return a temporary GET URL for the R2 object -- used by the
        read endpoint to redirect the caller directly to R2."""
        client = get_s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    @staticmethod
    def _build_path(
        env: str,
        group_id: str,
        chunk_index: int,
        attempt: int,
        job_id: str,
    ) -> str:
        return (
            f"{env}/{group_id}/"
            f"chunk_{chunk_index}/"
            f"attempt_{attempt}__{job_id}.log.gz"
        )
