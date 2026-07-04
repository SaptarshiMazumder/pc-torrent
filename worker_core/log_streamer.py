"""LogStreamer -- fire-and-forget worker log shipper.

Spawned by the fleet handler (vast/modal/community agent) as its own
subprocess.  Tails a log file the handler writes stdout to, and posts
the new bytes to the backend every ``tick_sec`` seconds.

Fire-and-forget: any HTTP failure is swallowed and the offset advances
anyway -- no retries, no local buffer, no worker-side state that can
grow unbounded.  Dropped chunks are lost forever; that is the accepted
trade for zero interference with the render path.

This module runs in its own OS process with its own memory and its own
PID.  It only reads a file the handler writes.  Blender's command line,
env, memory, GPU work, and output files are byte-identical to a run
without this module present.

Configuration is entirely via env vars set by the handler at spawn:

    PCR_LOG_FILE        path to the log file to tail (handler creates)
    PCR_LOG_ENDPOINT    signed URL for POST log-append (HMAC signs the
                        job_id, so job identity rides in the URL path)
    PCR_ENV             deployment env ("dev" / "prod")
    PCR_GROUP_ID        render_group_id (needed for Redis key routing)
    PCR_LOG_TICK_SEC    tick interval, default 10

SIGTERM handling: sets a stop flag, does one final tick to flush the
tail, then exits.  On SIGKILL (or container hard-destroy) the process
just dies with whatever the last tick got.
"""

from __future__ import annotations

import gzip
import logging
import os
import signal
import sys
import time

import requests


log = logging.getLogger("log_streamer")

DEFAULT_TICK_SEC = 10.0
POST_TIMEOUT_SEC = 3.0
FILE_WAIT_TIMEOUT_SEC = 30.0
FILE_WAIT_POLL_SEC = 0.2
SLEEP_SLICE_SEC = 0.25


class LogStreamer:

    def __init__(
        self,
        *,
        log_file_path: str,
        endpoint_url: str,
        env: str,
        group_id: str,
        tick_sec: float = DEFAULT_TICK_SEC,
    ) -> None:
        self._log_file_path = log_file_path
        self._endpoint_url = endpoint_url
        self._env = env
        self._group_id = group_id
        self._tick_sec = tick_sec
        self._last_offset = 0
        self._stop_requested = False

    def run(self) -> None:
        """Main loop.  Waits for the log file to appear, then ticks every
        ``tick_sec`` seconds until SIGTERM.  One final tick after the
        signal so the tail bytes are best-effort flushed."""
        self._install_signal_handlers()
        if not self._wait_for_log_file():
            log.warning(
                "log file %s never appeared after %.0fs; exiting",
                self._log_file_path, FILE_WAIT_TIMEOUT_SEC,
            )
            return
        log.info(
            "streaming %s -> %s (tick=%.1fs)",
            self._log_file_path,
            self._endpoint_url.split("?", 1)[0],
            self._tick_sec,
        )
        while not self._stop_requested:
            self._tick()
            self._sleep_or_stop(self._tick_sec)
        self._tick()
        log.info("exiting after final tick")

    def _tick(self) -> None:
        """Read the delta since last tick, POST it, advance offset.
        Every failure path is swallowed -- fire and forget."""
        try:
            current_size = os.path.getsize(self._log_file_path)
        except OSError as exc:
            log.warning("stat %s failed: %s", self._log_file_path, exc)
            return
        if current_size <= self._last_offset:
            return
        chunk = self._read_delta(current_size)
        offset_at_send = self._last_offset
        self._last_offset = current_size
        if chunk:
            self._post_chunk(chunk, offset_at_send)

    def _read_delta(self, current_size: int) -> bytes:
        try:
            with open(self._log_file_path, "rb") as f:
                f.seek(self._last_offset)
                return f.read(current_size - self._last_offset)
        except OSError as exc:
            log.warning("read %s failed: %s", self._log_file_path, exc)
            return b""

    def _post_chunk(self, chunk: bytes, offset: int) -> None:
        # env + group_id are needed on EVERY chunk so the backend can
        # build the Redis key.  The rest of the identity (attempt,
        # chunk_index, fleet, machine_id) is only needed on the first
        # chunk -- it populates the meta hash once.
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "gzip",
            "X-Chunk-Offset": str(offset),
            "X-Env": self._env,
            "X-Group-Id": self._group_id,
        }
        try:
            gzipped = gzip.compress(chunk)
            requests.post(
                self._endpoint_url,
                data=gzipped,
                headers=headers,
                timeout=POST_TIMEOUT_SEC,
            )
        except Exception as exc:
            # Fire and forget.  Offset has already advanced; these bytes
            # are now lost -- accepted trade for zero worker-side state.
            log.debug(
                "post failed (offset=%d, %d bytes): %s",
                offset, len(chunk), exc,
            )

    def _wait_for_log_file(self) -> bool:
        deadline = time.monotonic() + FILE_WAIT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if os.path.exists(self._log_file_path):
                return True
            if self._stop_requested:
                return False
            time.sleep(FILE_WAIT_POLL_SEC)
        return False

    def _sleep_or_stop(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(SLEEP_SLICE_SEC, remaining))

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("caught signal %d; final tick then exit", signum)
            self._stop_requested = True
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[log_streamer] %(message)s",
        stream=sys.stderr,
    )
    log_file = os.environ.get("PCR_LOG_FILE", "").strip()
    endpoint = os.environ.get("PCR_LOG_ENDPOINT", "").strip()
    if not log_file or not endpoint:
        log.warning(
            "PCR_LOG_FILE and/or PCR_LOG_ENDPOINT not set; log streaming "
            "disabled for this run",
        )
        return
    LogStreamer(
        log_file_path=log_file,
        endpoint_url=endpoint,
        env=os.environ.get("PCR_ENV", ""),
        group_id=os.environ.get("PCR_GROUP_ID", ""),
        tick_sec=float(
            os.environ.get("PCR_LOG_TICK_SEC", "") or DEFAULT_TICK_SEC
        ),
    ).run()


if __name__ == "__main__":
    main()
