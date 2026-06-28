"""IncrementalOutputUploader — upload output frames as they appear.

Scans ``output_dir`` every ``OUTPUT_SCAN_INTERVAL`` seconds.  A file is
"ready" after we observe it with the same size on two consecutive
scans (Blender finishes writing a frame before we see its final size).
Ready files are uploaded via :meth:`BackendClient.request_upload_urls`
+ direct R2 PUT, then registered via :meth:`BackendClient.register_outputs`.

:meth:`flush_final` runs one non-stability-gated scan to upload any
trailing frames after the render has stopped.

Idempotent w.r.t. duplicate uploads — already-uploaded filenames are
tracked and skipped.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import requests

from worker_core.backend_client import BackendClient

log = logging.getLogger(__name__)


class IncrementalOutputUploader:

    OUTPUT_SCAN_INTERVAL_SEC = float(os.getenv("OUTPUT_SCAN_INTERVAL", "1.0"))

    def __init__(
        self, client: BackendClient, job_id: str, output_dir: str,
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._output_dir = output_dir
        self._uploaded: set[str] = set()
        self._last_sizes: dict[str, int] = {}
        self._stable_counts: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def uploaded(self) -> list[str]:
        return sorted(self._uploaded)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"output-uploader-{self._job_id[:8]}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def flush_final(self) -> None:
        """Render process has stopped — upload everything remaining
        without waiting for size-stability."""
        self._scan_once(require_stable=False)

    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once(require_stable=True)
            except Exception as exc:
                log.warning(
                    f"Incremental upload scan failed for {self._job_id}: {exc}"
                )
            self._stop.wait(self.OUTPUT_SCAN_INTERVAL_SEC)

    def _candidate_files(self) -> list[str]:
        try:
            return sorted(
                f for f in os.listdir(self._output_dir)
                if not f.startswith(".")
                and os.path.isfile(os.path.join(self._output_dir, f))
            )
        except FileNotFoundError:
            return []

    def _primary_basenames(self) -> set[str]:
        """Main-render basenames the render driver wrote to the manifest
        (``.primary_outputs.json``).  Empty set on any read error -> every
        is_primary goes False and the backend falls back to its naming
        heuristic."""
        path = os.path.join(self._output_dir, ".primary_outputs.json")
        try:
            with open(path) as fh:
                data = json.load(fh)
            return set(data) if isinstance(data, list) else set()
        except (FileNotFoundError, ValueError, OSError):
            return set()

    def _scan_once(self, require_stable: bool) -> None:
        ready: list[str] = []
        for fname in self._candidate_files():
            if fname in self._uploaded:
                continue

            fpath = os.path.join(self._output_dir, fname)
            try:
                size = os.path.getsize(fpath)
            except OSError:
                continue
            if size <= 0:
                continue

            last_size = self._last_sizes.get(fname)
            if last_size == size:
                self._stable_counts[fname] = self._stable_counts.get(fname, 0) + 1
            else:
                self._stable_counts[fname] = 0
            self._last_sizes[fname] = size

            if not require_stable or self._stable_counts.get(fname, 0) >= 1:
                ready.append(fname)

        if ready:
            self._upload_batch(ready)

    def _upload_batch(self, filenames: list[str]) -> None:
        urls = self._client.request_upload_urls(filenames)

        uploaded_now: list[str] = []
        for fname in filenames:
            if fname in self._uploaded:
                continue
            url = urls.get(fname)
            if not url:
                log.warning(f"No presigned URL returned for {fname}")
                continue

            fpath = os.path.join(self._output_dir, fname)
            try:
                with open(fpath, "rb") as fh:
                    put_resp = requests.put(
                        url,
                        data=fh,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=600,
                    )
                    put_resp.raise_for_status()
            except Exception as exc:
                log.warning(f"Failed uploading frame {fname}: {exc}")
                continue

            uploaded_now.append(fname)

        if not uploaded_now:
            return

        primary = self._primary_basenames()
        self._client.register_outputs(
            [(name, name in primary) for name in uploaded_now]
        )

        for fname in uploaded_now:
            self._uploaded.add(fname)
            self._last_sizes.pop(fname, None)
            self._stable_counts.pop(fname, None)
        log.info(
            f"Registered {len(uploaded_now)} incremental output file(s) "
            f"(total uploaded: {len(self._uploaded)})"
        )
