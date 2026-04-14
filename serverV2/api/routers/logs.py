"""SSE log streaming + recent logs endpoint."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from fastapi import APIRouter

router = APIRouter(tags=["logs"])

MAX_RECENT_LOGS = 500
_recent_logs: deque[dict] = deque(maxlen=MAX_RECENT_LOGS)


class SSELogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "timestamp": self.format(record).split(" ")[0] if " " in self.format(record) else "",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        _recent_logs.append(entry)


@router.get("/logs/recent")
def recent_logs(limit: int = 100):
    return list(_recent_logs)[-limit:]


@router.get("/logs/stream")
async def log_stream():
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
