"""Validate at boot that every Modal endpoint declared in config.json has
a matching ``@app.function`` deployed on Modal.

Single source of truth is ``config.json``.  If somebody adds a GPU type
here but forgets to define + deploy the corresponding web endpoint in
``modal_worker/app.py``, dispatch will 404 at runtime.  This check fails
the server loudly at boot instead, so the drift is obvious.

Network / Modal-side outages are tolerated (logged, not raised) — we only
fail hard when Modal actively tells us the function doesn't exist.
"""

from __future__ import annotations

import logging

import httpx

from serverV2.config import ModalConfig

log = logging.getLogger(__name__)


def validate_modal_endpoints(config: ModalConfig) -> None:
    if not config.is_enabled():
        return
    for ep in config.endpoints:
        url = config.endpoint_url(ep.gpu_type)
        try:
            resp = httpx.get(url, timeout=5.0, follow_redirects=False)
        except httpx.ConnectError as exc:
            log.warning(
                "Modal endpoint %s unreachable at boot (%s) — skipping check",
                url, exc,
            )
            continue
        except httpx.TimeoutException:
            log.warning(
                "Modal endpoint %s timed out at boot — skipping check", url,
            )
            continue
        if resp.status_code == 404:
            raise RuntimeError(
                f"Modal endpoint for gpu_type={ep.gpu_type!r} does not exist "
                f"(404 at {url}). config.json declares it but Modal has not "
                f"deployed a matching @app.function.  Either remove the entry "
                f"from config.json `modal_instances` or run "
                f"`modal deploy modal_worker/app.py` with the matching "
                f"function defined."
            )
        log.info(
            "Modal endpoint ok: gpu_type=%s url=%s (HTTP %d)",
            ep.gpu_type, url, resp.status_code,
        )
