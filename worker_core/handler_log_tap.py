"""HandlerLogTap -- helper for wiring log streaming into a fleet handler.

Instantiated once per job by the handler.  When ``PCR_LOG_ENDPOINT`` is
set in the process env (server dispatch stamps it there together with
the signed HMAC url), spawns ``worker_core.log_streamer`` as a separate
subprocess with a per-job log file path, and exposes ``write(line)`` so
the handler's stdout-read loop can tap each Blender line to that file.

When ``PCR_LOG_ENDPOINT`` is unset -- e.g. the dispatch layer hasn't been
updated yet -- every method is a no-op.  The worker stays fully
backwards-compatible with an older server.

Byte-similar rendering guarantee:
    Blender is a subprocess of the handler; it doesn't know this class
    exists.  The tap adds one buffered=0 file write per line inside the
    handler's stdout loop -- microseconds per line, orders of magnitude
    faster than the loop already runs.  log_streamer is a separate OS
    process that only reads a file the handler writes.  Nothing in this
    module can affect Blender's command, env, memory, GPU work, or
    output files.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from typing import BinaryIO, Optional

from worker_core import log_streamer as _log_streamer_module


log = logging.getLogger(__name__)

# Absolute path to the log_streamer script.  Resolved once at import time
# via Python's own import machinery so the subprocess launcher does not
# depend on cwd / PYTHONPATH -- avoids ModuleNotFoundError in fleets where
# the subprocess starts with a different sys.path than the parent handler
# (Modal in particular resets sys.path per subprocess).
_LOG_STREAMER_SCRIPT = _log_streamer_module.__file__


class HandlerLogTap:

    def __init__(self, *, job_id: str) -> None:
        self._file: Optional[BinaryIO] = None
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[str] = None
        endpoint = os.environ.get("PCR_LOG_ENDPOINT", "").strip()
        if not endpoint or not job_id:
            return
        path = os.path.join(
            tempfile.gettempdir(), f"pcr_worker_{job_id}.log",
        )
        try:
            handle = open(path, "ab", buffering=0)
        except OSError as exc:
            log.warning(
                "HandlerLogTap: file open failed (%s); tap disabled", exc,
            )
            return
        try:
            # stdout/stderr inherited so log_streamer's own diagnostics
            # (``[log_streamer] streaming ...``, POST failures, etc.)
            # surface in the fleet's log stream.  They flow into the
            # handler's stdout/stderr, NOT into the render subprocess's
            # PIPE that HandlerLogTap captures -- the tap only reads
            # from the render's dedicated pipe, so this cannot pollute
            # what gets shipped as the "worker log".
            proc = subprocess.Popen(
                [sys.executable, _LOG_STREAMER_SCRIPT],
                env={**os.environ, "PCR_LOG_FILE": path},
            )
        except Exception as exc:
            log.warning(
                "HandlerLogTap: streamer spawn failed (%s); tap disabled",
                exc,
            )
            try:
                handle.close()
            except OSError:
                pass
            return
        self._file = handle
        self._proc = proc
        self._path = path
        log.info("HandlerLogTap active: path=%s pid=%s", path, proc.pid)

    def write(self, line: str) -> None:
        if self._file is None:
            return
        try:
            self._file.write(line.encode("utf-8", errors="replace"))
        except OSError:
            pass

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def __enter__(self) -> "HandlerLogTap":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()
