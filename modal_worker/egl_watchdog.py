"""EGLWatchdog — kill Blender only if EGL/OpenGL errors persist without
render progress.

Blender can emit a burst of EGL errors during EEVEE probing and still
recover (e.g. by falling back to CYCLES).  Killing on the first occurrence
aborts otherwise-healthy renders.  Instead we arm a grace timer when the
first wedge pattern appears; any PCR_PROGRESS event disarms it.  If the
timer expires with no progress the container is genuinely stuck, so we
kill the Blender process ourselves.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from modal_worker.process_group_killer import kill_process_group

log = logging.getLogger(__name__)

# Substrings in Blender stdout that indicate a potentially-stuck EGL/OpenGL
# context.  render.sh already converts other fatal signals (RENDER_DRIVER
# ERROR, "Cannot render, no camera", etc.) into a nonzero exit code, so we
# intentionally do NOT list them here.
WEDGE_PATTERNS: tuple[str, ...] = (
    "EGL_BAD_MATCH",
    "EGL_BAD_DISPLAY",
    "EGL_NOT_INITIALIZED",
    "Failed to create OpenGL context",
)


class EGLWatchdog:

    def __init__(self, proc: subprocess.Popen, window_sec: float) -> None:
        self._proc = proc
        self._window_sec = window_sec
        self._lock = threading.Lock()
        self._first_error_at: float | None = None
        self._first_error_line: str = ""
        self._stop = threading.Event()
        self._fired = False
        self._thread: threading.Thread | None = None

    @property
    def fired(self) -> bool:
        return self._fired

    @property
    def first_error_line(self) -> str:
        return self._first_error_line

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="egl-watchdog",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def note_wedge_pattern(self, line: str) -> None:
        with self._lock:
            if self._first_error_at is not None:
                return
            self._first_error_at = time.monotonic()
            self._first_error_line = line
        log.warning(
            "EGL/OpenGL wedge pattern detected, arming %.0fs watchdog: %s",
            self._window_sec,
            line,
        )

    def note_progress(self) -> None:
        with self._lock:
            if self._first_error_at is None:
                return
            self._first_error_at = None
            self._first_error_line = ""
        log.info("Render progress received — disarming EGL watchdog")

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                if self._first_error_at is None:
                    continue
                elapsed = time.monotonic() - self._first_error_at
                if elapsed < self._window_sec:
                    continue
                line = self._first_error_line
            log.error(
                "EGL watchdog firing: no render progress for %.0fs after '%s', "
                "killing Blender process group",
                elapsed,
                line,
            )
            self._fired = True
            kill_process_group(self._proc)
            return
