"""Minimal Vast.ai HTTP client for the backup monitor.

The backup monitor only needs to enumerate currently-rented instances on
the account so it can match them against our jobs table.  Cancellation
is delegated to the orchestrator over HTTP, so this client never needs
to call DELETE -- list-only.

Kept intentionally separate from the serverV2 ``VastClient``: backup
monitor has its own deploy + requirements; importing serverV2 would
drag the whole orchestrator into this Cloud Run Job's image.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://console.vast.ai/api/v0"


class VastApiClient:

    def __init__(
        self,
        *,
        api_key: str,
        http_timeout_sec: int = 30,
        api_base: str = _DEFAULT_API_BASE,
    ) -> None:
        self._api_key = api_key
        self._timeout = http_timeout_sec
        self._api_base = api_base.rstrip("/")

    def list_instances(self) -> list[dict[str, Any]]:
        """Return every instance currently rented on this account.

        Returns ``[]`` on transport errors so the caller can log + skip;
        a tick failure should never crash the backup monitor.
        """
        try:
            resp = requests.get(
                f"{self._api_base}/instances/",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                params={"owner": "me"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Vast list_instances failed: %s", exc)
            return []
        try:
            data = resp.json()
        except ValueError:
            log.warning("Vast list_instances: response was not JSON")
            return []
        return list(data.get("instances") or [])
