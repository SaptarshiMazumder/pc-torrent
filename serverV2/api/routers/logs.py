"""SSE log streaming + recent logs endpoint.

Admin-only: server logs routinely carry user ids, filenames, and infra
detail, so both routes are gated by ``require_admin``.  The SSE route
accepts the token as a ``?token=`` query param (EventSource cannot set
headers); ``get_current_user`` already supports that.

Note: the ring buffer is per-process memory.  With multiple Cloud Run
instances each request sees only the logs of the instance that served it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from serverV2.api.dependencies import require_admin

router = APIRouter(tags=["logs"])

MAX_RECENT_LOGS = 500
_recent_logs: deque[dict] = deque(maxlen=MAX_RECENT_LOGS)


class SSELogHandler(logging.Handler):
    """Ring-buffer handler backing /logs/recent + /logs/stream.  Attached to
    the root logger at startup (see ``install_log_capture``).  ``emit`` must
    never raise — a logging path that can crash the app is worse than a
    dropped log line."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc,
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            _recent_logs.append(entry)
        except Exception:  # pragma: no cover - logging must never break callers
            pass


def install_log_capture(level: int = logging.INFO) -> None:
    """Attach a single ``SSELogHandler`` to the root logger so the dashboard's
    log panel actually fills.  Idempotent — safe to call on every startup /
    reload without stacking duplicate handlers."""
    root = logging.getLogger()
    if any(isinstance(h, SSELogHandler) for h in root.handlers):
        return
    handler = SSELogHandler()
    handler.setLevel(level)
    root.addHandler(handler)
    # Ensure records at INFO actually reach handlers even if the root level
    # was left at WARNING by the hosting environment.
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)


@router.get("/logs/recent")
def recent_logs(limit: int = 100, _admin: dict = Depends(require_admin)):
    return list(_recent_logs)[-limit:]


@router.get("/logs/stream")
async def log_stream(_admin: dict = Depends(require_admin)):
    from sse_starlette.sse import EventSourceResponse
    import json

    async def _generate():
        idx = len(_recent_logs)
        while True:
            current = len(_recent_logs)
            while idx < current:
                try:
                    entry = _recent_logs[idx]
                    yield {"data": json.dumps(entry)}
                except IndexError:
                    pass
                idx += 1
            await asyncio.sleep(1)

    return EventSourceResponse(_generate())
