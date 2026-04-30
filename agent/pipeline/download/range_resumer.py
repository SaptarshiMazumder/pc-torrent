"""RangeResumer -- HTTP GET with Range-resume on mid-stream drops.

Wraps requests.get(stream=True) + iter_content() with an outer
retry-from-byte-N loop that re-issues the GET with a
``Range: bytes=N-`` header whenever ChunkedEncodingError /
ConnectionError / Timeout interrupts the stream.  R2 (and any S3-
compatible backend) supports Range, so the resumed download appends
to the file instead of restarting from byte 0.

Duplicated from cloud_worker/scripts/workflow/download/range_resumer.py
on purpose -- the two run in different deployment artifacts (Tauri
sidecar vs Docker image) so a shared Python package would cost more
in build complexity than it saves in code duplication here.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

_DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024     # 8 MB
_DEFAULT_TIMEOUT_SEC = 300
_DEFAULT_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SEC = 2.0


class RangeResumer:

    def __init__(
        self,
        *,
        chunk_size: int = _DEFAULT_CHUNK_BYTES,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._chunk = chunk_size
        self._timeout = timeout_sec
        self._max_attempts = max_attempts

    def download(
        self,
        url: str,
        dest_path: str,
        *,
        on_progress: Callable[[int], None] | None = None,
    ) -> int:
        """Download ``url`` to ``dest_path``.  Returns total bytes
        written.  ``on_progress(n)`` invoked per chunk (n = chunk size,
        not cumulative).  Raises RuntimeError after max_attempts."""
        attempt = 0
        downloaded = 0
        while True:
            headers: dict[str, str] = {}
            mode = "wb"
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
                mode = "ab"
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    allow_redirects=True,
                    stream=True,
                )
                resp.raise_for_status()
                # Server returns 200 if it ignored Range; restart from 0.
                if mode == "ab" and resp.status_code == 200:
                    log.warning(
                        "Server returned 200 to Range request; "
                        "restarting from byte 0",
                    )
                    downloaded = 0
                    mode = "wb"
                with open(dest_path, mode) as f:
                    for block in resp.iter_content(chunk_size=self._chunk):
                        if not block:
                            continue
                        f.write(block)
                        downloaded += len(block)
                        if on_progress:
                            on_progress(len(block))
                return downloaded
            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                attempt += 1
                if attempt >= self._max_attempts:
                    raise RuntimeError(
                        f"Download failed after {attempt} attempts at "
                        f"{downloaded} bytes: {exc}"
                    ) from exc
                wait = _BACKOFF_BASE_SEC * attempt
                log.warning(
                    f"Download interrupted at {downloaded} bytes "
                    f"(attempt {attempt}/{self._max_attempts}); "
                    f"resuming after {wait:.1f}s -- {type(exc).__name__}: {exc}"
                )
                time.sleep(wait)
