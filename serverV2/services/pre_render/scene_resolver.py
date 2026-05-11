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
        render_section = normalized_overrides.setdefault("render", {})
        render_section["engine"] = engine
        heaviness["render_engine"] = engine

        # Per-field merge for every other override-able render setting.
        # For each field: pick the user override if set, else the
        # analyzer's snapshot value, then stamp the resolved value into
        # the consumer dict(s) that read it.  Same one-value-two-homes
        # pattern as engine, but only for fields BOTH consumers actually
        # use -- worker-only fields (cycles_denoise, device_policy)
        # stay in render_overrides only, and planner-only stats
        # (geometry / assets / features) stay in heaviness only.
        def _pick(override, default):
            return override if override is not None else default

        # Resolution components -- worker uses each component to set
        # scene.render.*; planner only uses effective_pixels, which
        # gets re-derived below from the same resolved components.
        res_x = _pick(render_section.get("resolution_x"), heaviness.get("resolution_x"))
        res_y = _pick(render_section.get("resolution_y"), heaviness.get("resolution_y"))
        res_pct = _pick(
            render_section.get("resolution_percentage"),
            heaviness.get("resolution_percentage"),
        )
        render_section["resolution_x"] = res_x
        render_section["resolution_y"] = res_y
        render_section["resolution_percentage"] = res_pct
        heaviness["resolution_x"] = res_x
        heaviness["resolution_y"] = res_y
        heaviness["resolution_percentage"] = res_pct
        if isinstance(res_x, int) and isinstance(res_y, int):
            pct = res_pct if isinstance(res_pct, int) else 100
            heaviness["effective_pixels"] = int(res_x * res_y * (pct / 100.0) ** 2)

        # Samples -- worker reads render.cycles_samples; planner reads
        # heaviness.samples.  Resolve once, stamp both sides.
        samples = _pick(render_section.get("cycles_samples"), heaviness.get("samples"))
        render_section["cycles_samples"] = samples
        heaviness["samples"] = samples

        # Adaptive sampling -- worker reads render.cycles_adaptive_sampling;
        # planner reads heaviness.uses_adaptive_sampling.
        adaptive = _pick(
            render_section.get("cycles_adaptive_sampling"),
            heaviness.get("uses_adaptive_sampling"),
        )
        render_section["cycles_adaptive_sampling"] = adaptive
        heaviness["uses_adaptive_sampling"] = adaptive

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
