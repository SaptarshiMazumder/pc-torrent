"""Download: blend-file fetch with HTTP-Range resume on connection drops.

RangeResumer    -- low-level transport: GET stream + Range-resume retry.
                   Same logic as cloud_worker/scripts/workflow/download/.
                   Duplicated intentionally -- agent and cloud_worker are
                   separate deployment artifacts (Tauri sidecar vs Docker
                   image) and can't share Python modules cleanly.
"""

from .range_resumer import RangeResumer

__all__ = ["RangeResumer"]
