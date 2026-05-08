"""HTTP POST adapter that reports ghost provider-side instances to the
main orchestrator.

Mirrors ``OrphanReporter``: same shared ``X-Orphan-Secret`` header,
same fault-tolerant logging, same per-call idempotency contract on
the server side.  The orchestrator decides whether to actually
destroy / cancel -- it re-reads the row's current state and refuses
if the row is somehow active again.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class GhostReporter:

    def __init__(
        self,
        *,
        orchestrator_url: str,
        orphan_secret: str,
        http_timeout_sec: int,
    ) -> None:
        self._url_base = orchestrator_url.rstrip("/")
        self._secret = orphan_secret
        self._timeout = http_timeout_sec

    def report_vast_ghost(self, vast_id: int, *, reason: str) -> None:
        self._post(
            f"/internal/ghost-instance/vast/{vast_id}",
            payload={"reason": reason},
            descr=f"Vast instance {vast_id}",
        )

    def report_modal_ghost(self, call_id: str, *, reason: str) -> None:
        self._post(
            f"/internal/ghost-instance/modal/{call_id}",
            payload={"reason": reason},
            descr=f"Modal call {call_id}",
        )

    def _post(self, path: str, *, payload: dict, descr: str) -> None:
        url = f"{self._url_base}{path}"
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"X-Orphan-Secret": self._secret},
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                log.info("Reported ghost %s -- orchestrator accepted", descr)
            else:
                log.warning(
                    "Ghost report for %s returned HTTP %d: %s",
                    descr, resp.status_code, resp.text[:200],
                )
        except requests.RequestException as exc:
            log.warning("Ghost report for %s failed: %s", descr, exc)
