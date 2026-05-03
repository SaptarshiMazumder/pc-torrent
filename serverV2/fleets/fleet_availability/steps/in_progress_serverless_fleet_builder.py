"""InProgressServerlessFleetBuilder — live in-flight count per serverless fleet.

Computes the ``{fleet -> in_flight_count}`` dict the allocators use for
headroom-based mix selection (``fleet_max_parallel - in_flight``).
One DB query (``JobRepository.count_active_by_fleet``).

Hardcoded to the two serverless fleets we have today (``vast_serverless``,
``modal_serverless``).  When a new serverless fleet lands (e.g.
RunPod), add it explicitly here -- defensive ``endswith('_serverless')``
filtering would mask typos in fleet names.
"""

from __future__ import annotations

from serverV2.repositories.job_repository import JobRepository


class InProgressServerlessFleetBuilder:

    def __init__(self, *, job_repo: JobRepository) -> None:
        self._jobs = job_repo

    def build(self) -> dict[str, int]:
        counts = self._jobs.count_active_by_fleet()
        return {
            "vast_serverless":  counts.get("vast_serverless", 0),
            "modal_serverless": counts.get("modal_serverless", 0),
        }
