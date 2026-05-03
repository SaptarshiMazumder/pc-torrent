"""AllocationEngineResolver — extract the render engine string from a
JSON-encoded overrides payload.

One responsibility: parse ``render.engine`` out of the overrides JSON.
Used by the dispatch handler when re-building a ``DispatchContext``
from a queue row.
"""

from __future__ import annotations

import json


class AllocationEngineResolver:

    @staticmethod
    def from_overrides_json(render_overrides_json: str | None) -> str | None:
        """Return ``render.engine`` from a JSON-encoded overrides payload,
        or ``None`` if the payload is empty.  Raises on malformed JSON —
        at this point in the pipeline the payload was produced by trusted
        upstream code, so a parse failure is a real bug and should
        surface."""
        if not render_overrides_json:
            return None
        parsed = json.loads(render_overrides_json)
        if not isinstance(parsed, dict):
            return None
        render_section = parsed.get("render")
        if not isinstance(render_section, dict):
            return None
        engine = render_section.get("engine")
        return engine if isinstance(engine, str) and engine else None
