"""HTTP POST adapter that tells the orchestrator to flush ready
worker logs from Redis into R2.

Backup_monitor stays a DUMB scheduler: this class has no Redis client,
no R2 client, no DB access.  All storage-schema knowledge lives inside
the orchestrator's ``JobsLoggerService``; this trigger just fires the
endpoint on every 60 s tick.  Change the Redis format or R2 layout and
this file does not need touching.

Auth is the shared ``X-Orphan-Secret`` header already used by every
other backup_monitor -> orchestrator call.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class LogWriteTrigger:

    def __init__(
        self,
        *,
        orchestrator_url: str,
        orphan_secret: str,
        http_timeout_sec: int,
    ) -> None:
        self._url = (
            f"{orchestrator_url.rstrip('/')}/internal/write-logs-to-r2"
        )
        self._secret = orphan_secret
        self._timeout = http_timeout_sec

    def tick(self) -> None:
        try:
            resp = requests.post(
                self._url,
                headers={"X-Orphan-Secret": self._secret},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            log.warning("write-logs-to-r2 call failed: %s", exc)
            return
        if resp.status_code != 200:
            log.warning(
                "write-logs-to-r2 returned HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return
        try:
            written = int(resp.json().get("written", 0))
        except (ValueError, TypeError):
            written = 0
        log.info("write-logs-to-r2: %d log(s) moved to R2", written)
