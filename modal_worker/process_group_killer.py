"""SIGKILL a process group — the bash subprocess and every descendant.

`proc.kill()` only signals bash itself, leaving grandchildren (Blender,
render_driver, ...) alive and holding the stdout pipe open, which wedges
the handler's read loop.  Popen is created with ``start_new_session=True``
so ``proc.pid`` is the process-group id we can target with ``killpg``.

Shared utility used by the watchdogs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess

log = logging.getLogger(__name__)


def kill_process_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.warning(f"Could not resolve process group for pid {proc.pid}: {exc}")
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            log.warning(
                f"killpg({pgid}, SIGKILL) failed: {exc}; falling back to proc.kill()"
            )

    try:
        proc.kill()
    except Exception as exc:
        log.warning(f"proc.kill() fallback failed: {exc}")
