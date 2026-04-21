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

from serverV2.core.value_objects import output_frame_sort_key, parse_output_files


# (r2_key, download_name) -> presigned URL
PresignerFn = Callable[[str, str], str]


class OutputsResolver:

    def __init__(self, presigner: PresignerFn) -> None:
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
        output_files = parse_output_files(job.get("output_files"))
        sorted_files = sorted(output_files, key=output_frame_sort_key)
        job_id = job["id"]
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
