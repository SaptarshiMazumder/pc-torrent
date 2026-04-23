"""MemoryWatchdog — kill Blender before the kernel OOM-kills the container.

If MemAvailable drops below ``min_free_fraction`` of MemTotal, SIGKILL the
Blender process group so Python stays alive long enough to run the normal
error-return path.  Without this, a kernel OOM kill takes Python out too,
Modal sees "Runner failed with exit code: 2", and re-queues the same input
on a new container — bypassing our ``retries=0`` intent.
"""

from __future__ import annotations

import logging
import subprocess
import threading

from modal_worker.process_group_killer import kill_process_group

log = logging.getLogger(__name__)


class MemoryWatchdog:

    def __init__(
        self,
        proc: subprocess.Popen,
        min_free_fraction: float,
        poll_sec: float,
    ) -> None:
        self._proc = proc
        self._min_free_fraction = min_free_fraction
        self._poll_sec = poll_sec
        self._stop = threading.Event()
        self._fired = False
        self._reason: str = ""
        self._thread: threading.Thread | None = None

    @property
    def fired(self) -> bool:
        return self._fired

    @property
    def reason(self) -> str:
        return self._reason

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mem-watchdog",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _read_meminfo(self) -> tuple[int, int] | None:
        try:
            with open("/proc/meminfo", "r") as f:
                data = f.read()
        except OSError:
            return None
        total_kb = 0
        avail_kb = 0
        for line in data.splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
            if total_kb and avail_kb:
                break
        if total_kb <= 0 or avail_kb <= 0:
            return None
        return total_kb, avail_kb

    def _run(self) -> None:
        while not self._stop.wait(self._poll_sec):
            snapshot = self._read_meminfo()
            if snapshot is None:
                continue
            total_kb, avail_kb = snapshot
            free_fraction = avail_kb / total_kb
            if free_fraction >= self._min_free_fraction:
                continue
            avail_gb = avail_kb / (1024 * 1024)
            total_gb = total_kb / (1024 * 1024)
            self._reason = (
                f"MemAvailable={avail_gb:.1f}GB / MemTotal={total_gb:.1f}GB "
                f"({free_fraction * 100:.1f}% < {self._min_free_fraction * 100:.0f}%)"
            )
            log.error(
                "Memory watchdog firing: %s — killing Blender process group",
                self._reason,
            )
            self._fired = True
            kill_process_group(self._proc)
            return
