"""Output entries builder + URL resolver.

Given jobs, produces downloadable file entries enriched with:
  - preview_path: relative backend route (frontend resolves with its base URL)
  - url:          absolute presigned R2 URL (direct download, no backend hop)

Pure domain concern.  Storage presigning is injected as a callable so this
module depends on an abstraction, not on the storage infrastructure directly.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from serverV2.core.value_objects import output_frame_sort_key
from serverV2.repositories.output_frame_repository import OutputFrameRepository


# (r2_key, download_name) -> presigned URL
PresignerFn = Callable[[str, str], str]


class OutputsResolver:

    def __init__(
        self,
        *,
        output_frame_repo: OutputFrameRepository,
        presigner: PresignerFn,
    ) -> None:
        self._output_frames = output_frame_repo
        self._presigner = presigner

    def entries(self, jobs: list[dict[str, Any]], scope_id: str) -> list[dict[str, Any]]:
        """Build enriched entries for every output file across the given jobs.

        ``scope_id`` is the group_id for render-groups or the job_id for
        standalone jobs — it's used as the R2 key prefix (``jobs/{scope}/output/*``).
        """
        result: list[dict[str, Any]] = []
        for job in jobs:
            result.extend(self._for_job(job, scope_id))
        return result

    def _for_job(self, job: dict[str, Any], scope_id: str) -> list[dict[str, Any]]:
        job_id = job["id"]
        # Per-job filenames come straight from the output_frames table.
        sorted_files = sorted(
            self._output_frames.list_for_job(job_id), key=output_frame_sort_key,
        )
        entries: list[dict[str, Any]] = []
        for filename in sorted_files:
            encoded = quote(filename, safe="")
            r2_key = f"jobs/{scope_id}/output/{filename}"
            entries.append({
                "filename": filename,
                "job_id": job_id,
                "group_id": job.get("group_id") or scope_id,
                "preview_path": f"/jobs/{job_id}/output/{encoded}/preview",
                "url": self._presigner(r2_key, filename),
            })
        return entries
