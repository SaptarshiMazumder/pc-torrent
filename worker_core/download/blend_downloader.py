"""BlendDownloader -- orchestrate URL -> file -> (extract zip if needed).

Doesn't pick which .blend to render against if the bundle has many --
that's the fleet handler's job.  This module only owns "fetch the
bytes and lay them out on disk".
"""

from __future__ import annotations

import logging
import os
import zipfile
from typing import Callable

from worker_core.download.range_resumer import RangeResumer

log = logging.getLogger(__name__)


class BlendDownloader:

    def __init__(self, *, resumer: RangeResumer | None = None) -> None:
        self._resumer = resumer or RangeResumer()

    def fetch(
        self,
        *,
        url: str,
        input_dir: str,
        filename: str,
        on_bytes: Callable[[int], None] | None = None,
    ) -> str:
        """Download ``url`` into ``input_dir/filename``.  If filename ends
        in ``.zip``, extract into ``input_dir`` and remove the archive.

        ``on_bytes(n)`` -- per-chunk progress callback (n = chunk size,
        not cumulative).  Wired to BytesProgress.add() at the call site.

        Returns the path of the on-disk artifact:
        - For a direct .blend upload: the .blend itself.
        - For a .zip upload: the directory (caller scans it for .blend
          files since a bundle may contain several).
        """
        os.makedirs(input_dir, exist_ok=True)
        dest = os.path.join(input_dir, filename)

        log.info(f"Downloading {filename} from {url}")
        total = self._resumer.download(url, dest, on_progress=on_bytes)
        log.info(f"Downloaded {filename} ({total / 1024 / 1024:.1f} MB)")

        if filename.lower().endswith(".zip"):
            self._extract_zip(dest, input_dir)
            return input_dir
        return dest

    def _extract_zip(self, zip_path: str, input_dir: str) -> None:
        log.info("Extracting zip archive")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(input_dir)
        os.remove(zip_path)
        log.info(f"Extracted contents: {os.listdir(input_dir)}")
