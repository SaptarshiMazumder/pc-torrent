"""lifecycle_output — helpers for output-layer concerns inside RenderLifecycle.

The lifecycle file became too large; this package extracts cohesive
sub-concerns out of it.  First entry: frame deduplication across
sibling attempts of a chunk.  Future moves: terminal snapshot writes,
output URL resolution, etc.

RenderLifecycle stays as the narrative orchestrator; these helpers do
the grunt computation.
"""

from serverV2.orchestrator.lifecycle_output.lifecycle_frames_deduplication import (
    LifecycleFramesDeduplication,
)

__all__ = ["LifecycleFramesDeduplication"]
