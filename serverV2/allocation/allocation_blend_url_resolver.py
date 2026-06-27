"""Resolves the blend file download URL.

Both serverless fleets call back to the same ``PUBLIC_BACKEND_URL`` (an env
secret), so the URL is fleet-independent; the ``fleet`` arg is kept for
interface stability with the dispatch path.
"""

from __future__ import annotations


class AllocationBlendUrlResolver:

    def __init__(self, public_backend_url: str) -> None:
        self._public_backend_url = public_backend_url

    def resolve(self, fleet: str, group_id: str, input_filename: str) -> str:
        return f"{self._public_backend_url}/render-groups/{group_id}/input/{input_filename}"
