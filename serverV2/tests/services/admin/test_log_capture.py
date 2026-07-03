"""Log-capture handler — install idempotency + ring-buffer fill.

Guards the fix for the previously-dead /logs panel: the SSELogHandler must
attach exactly once and actually capture emitted records.
"""

from __future__ import annotations

import logging

from serverV2.api.routers import logs as logs_mod
from serverV2.api.routers.logs import SSELogHandler, install_log_capture


def _count_handlers():
    return sum(isinstance(h, SSELogHandler) for h in logging.getLogger().handlers)


def test_install_is_idempotent():
    install_log_capture()
    install_log_capture()
    install_log_capture()
    assert _count_handlers() == 1


def test_emitted_records_land_in_the_ring_buffer():
    install_log_capture()
    logs_mod._recent_logs.clear()
    logging.getLogger("test.capture").warning("hello dashboard %d", 42)
    entries = list(logs_mod._recent_logs)
    assert any(e["message"] == "hello dashboard 42" and e["level"] == "WARNING" for e in entries)
    # timestamp is a real ISO string, not the old space-split artifact.
    assert entries[-1]["timestamp"].count("-") >= 2
