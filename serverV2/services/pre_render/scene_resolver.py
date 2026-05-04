"""SceneResolver — merges analyzer snapshot + user overrides at the boundary.

The render group has two upstream sources of "scene context":

* ``analysis_snapshot``  — what the desktop-app's local Blender analyzer
                            extracted from the .blend (heaviness fields:
                            polygons, materials, samples, render_engine, ...)
* ``render_overrides``   — what the user edited in the UI on top of those
                            defaults (timeline, output, camera_ranges,
                            possibly an explicit render.engine override, ...)

Pre-submit code (estimate / future re-analyze) keeps the two shapes
side-by-side because the user can still edit overrides and recompute.
At the submit boundary they MUST collapse to one canonical shape so
every post-submit reader (lifecycle, allocator, retry pipeline,
worker dispatch) reads a single source of truth.

This class is the only place in the codebase that knows the merge
rule.  Output shape::

    {
        "render_overrides": { ...normalized overrides, with
                              render.engine guaranteed populated... },
        "heaviness":        { ...heaviness fields from snapshot,
                              with render_engine guaranteed populated... },
    }

Engine resolution rule: explicit ``render_overrides.render.engine`` wins
over the analyzer's ``snapshot.heaviness.render_engine``.  If neither
source has it, ``resolve`` raises — that's a real upload bug, not
something to silently fall back on.
"""

from __future__ import annotations

import json
from typing import Any

from serverV2.core.value_objects import (
    normalize_render_overrides,
    parse_analysis_heaviness,
)


class SceneResolutionError(ValueError):
    """Raised when the merged scene context lacks a required field
    (engine today; could expand to other strict fields later)."""


class SceneResolver:

    def resolve(
        self,
        *,
        analysis_snapshot: dict[str, Any] | None,
        render_overrides: dict[str, Any] | None,
        file_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Build the resolved scene blob.  Pure function; no I/O.

        ``file_size_bytes`` is the server-side R2 fact (the analyzer
        doesn't know it).  Stamped into the heaviness section so
        downstream cost / time analysers see one complete dict.
        """
        normalized_overrides = normalize_render_overrides(render_overrides)
        heaviness = parse_analysis_heaviness(
            analysis_snapshot,
            file_size_bytes=file_size_bytes,
        )

        engine = self._resolve_engine(normalized_overrides, heaviness)
        # Mirror the resolved engine into both sub-blobs so downstream
        # readers don't have to know about the merge rule.  Worker reads
        # ``render_overrides.render.engine``; strategies read
        # ``heaviness.render_engine`` (or ``engine`` kwarg unpacked from
        # the same source).  One value, two homes — by design.
        normalized_overrides.setdefault("render", {})["engine"] = engine
        heaviness["render_engine"] = engine

        return {
            "render_overrides": normalized_overrides,
            "heaviness": heaviness,
        }

    def serialize(self, resolved: dict[str, Any]) -> str:
        return json.dumps(resolved)

    def deserialize(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {"render_overrides": {}, "heaviness": {}}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {"render_overrides": {}, "heaviness": {}}
        return parsed

    @staticmethod
    def _resolve_engine(
        normalized_overrides: dict[str, Any],
        heaviness: dict[str, Any],
    ) -> str:
        render_section = normalized_overrides.get("render")
        if isinstance(render_section, dict):
            override = render_section.get("engine")
            if isinstance(override, str) and override.strip():
                return override.strip()
        snapshot_engine = heaviness.get("render_engine")
        if isinstance(snapshot_engine, str) and snapshot_engine.strip():
            return snapshot_engine.strip()
        raise SceneResolutionError(
            "Render engine could not be resolved.  Neither "
            "render_overrides.render.engine nor "
            "analysis_snapshot.heaviness.render_engine is populated."
        )
