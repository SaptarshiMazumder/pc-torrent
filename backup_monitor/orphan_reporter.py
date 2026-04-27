"""HTTP POST adapter that reports orphaned jobs to the main orchestrator."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class OrphanReporter:
    def __init__(
        self,
        orchestrator_url: str,
        orphan_secret: str,
        http_timeout_sec: int,
    ) -> None:
        self._url_base = orchestrator_url.rstrip("/")
        self._secret = orphan_secret
        self._timeout = http_timeout_sec

    def report(self, job_id: str, error: str) -> None:
        url = f"{self._url_base}/internal/orphan/{job_id}"
        try:
            resp = requests.post(
                url,
                json={"error": error},
                headers={"X-Orphan-Secret": self._secret},
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                log.info("Reported orphan %s — orchestrator accepted", job_id)
            else:
                log.warning(
                    "Orphan report for %s returned HTTP %d: %s",
                    job_id, resp.status_code, resp.text[:200],
                )
        except requests.RequestException as exc:
            log.warning("Orphan report for %s failed: %s", job_id, exc)
