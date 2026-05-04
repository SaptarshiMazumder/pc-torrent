"""Pre-submit (render-group) services — stateless RPCs.

This package owns every code path that runs BEFORE a render group is
submitted (i.e. before ``confirm_upload``).  Pre-submit data lives in
the request body (the desktop app analyses the .blend locally and
holds both the snapshot and the user's overrides) — nothing is
persisted from this layer.

Allowed inputs at this stage are the two raw shapes:
  * ``analysis_snapshot``  — analyzer output (immutable per .blend)
  * ``render_overrides``   — user-edited override blob

This package is the ONLY part of the codebase that handles those two
shapes side-by-side.  At the submit boundary (``RenderGroupService.
confirm_upload``) the merge happens once via :class:`SceneResolver`,
the merged result is persisted to ``render_groups.resolved_scene_json``,
and every post-submit reader sees a single canonical row.
"""

from serverV2.services.pre_render.scene_resolver import SceneResolver
from serverV2.services.pre_render.pre_render_estimator import (
    PreRenderEstimator,
    PreRenderEstimateRequest,
)
from serverV2.services.pre_render.frame_range_resolver import (
    resolve_frame_range,
)

__all__ = [
    "SceneResolver",
    "PreRenderEstimator",
    "PreRenderEstimateRequest",
    "resolve_frame_range",
]
