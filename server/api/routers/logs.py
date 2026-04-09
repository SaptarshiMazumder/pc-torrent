"""SSE log streaming router.

The in-memory log ring buffer and broadcast handler live here so they are
set up at import time (before any routes run) and can be referenced by the
SSE generator.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from firebase_auth import get_current_user

# ---------------------------------------------------------------------------
# In-memory log ring buffer + SSE broadcast
# ---------------------------------------------------------------------------

LOG_BUFFER_SIZE = 500
_log_buffer: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_SIZE)
_log_subscribers: list[asyncio.Queue] = []
_log_subscribers_lock = threading.Lock()


class _BroadcastHandler(logging.Handler):
    """Captures log records into a ring buffer and pushes to SSE subscribers."""

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        _log_buffer.append(line)
        with _log_subscribers_lock:
            for q in _log_subscribers:
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    pass  # slow consumer, drop line


def setup_log_broadcast() -> None:
    """Register the broadcast handler on the root logger. Call once at startup."""
    handler = _BroadcastHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter(tags=["logs"])


@router.get("/logs/stream")
async def stream_logs(_: dict = Depends(get_current_user)) -> Any:
    """SSE endpoint: streams application logs in real time."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)

    async def event_generator():
        for line in list(_log_buffer):
            yield {"data": line}
        with _log_subscribers_lock:
            _log_subscribers.append(q)
        try:
            while True:
                line = await q.get()
                yield {"data": line}
        except asyncio.CancelledError:
            pass
        finally:
            with _log_subscribers_lock:
                _log_subscribers.remove(q)

    return EventSourceResponse(event_generator())


@router.get("/logs/recent")
def recent_logs(_: dict = Depends(get_current_user)) -> list[str]:
    """Return the last N log lines as JSON array."""
    return list(_log_buffer)
