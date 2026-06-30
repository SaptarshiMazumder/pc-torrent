"""TelemetryParser -- consumes Blender stdout lines and accumulates the
data-richness telemetry that the worker forwards to the server.

Shared between the Vast, Modal, and community-agent handlers so the
parsing logic lives in exactly one place.  Each handler does:

    parser = TelemetryParser()
    for line in proc.stdout:
        parser.consume(line)
        # ... existing PCR_PROGRESS handling, etc.
    # After Blender exits:
    payload = parser.payload()
    if payload:
        client.push_telemetry(payload)

The parser reads two things from Blender's stdout:

    1.  ``PCR_TELEMETRY:{json}`` -- a single line emitted at end of render
        by ``telemetry_collector.TelemetryCollector`` (in-Blender Python).
        Carries the bpy / env-known fields (GPU device, blender version,
        denoiser, etc.).

    2.  Cycles ``Mem:NNN.NM (Peak: NNNN.NM)`` lines -- streamed
        throughout render.  We track the largest Peak value seen as
        ``peak_vram_mb``.  Cycles cannot self-report this from inside
        Python because it writes the line directly to C stdout.

The two streams are merged into one payload by ``payload()``.  Pure
parsing -- no side effects, no I/O, no network.
"""

from __future__ import annotations

import json
import re
from typing import Any


_PCR_TELEMETRY_PREFIX = "PCR_TELEMETRY:"
# Cycles status line:  "Mem:1234.56M (Peak 5678.90M) | ..."
_CYCLES_PEAK_RE = re.compile(
    r"Peak\s*:?\s*(\d+(?:\.\d+)?)\s*([MG])", re.IGNORECASE,
)


class TelemetryParser:

    def __init__(self) -> None:
        self._in_blender_payload: dict[str, Any] = {}
        self._peak_vram_mb: int | None = None

    def consume(self, line: str) -> None:
        """Feed one stdout line.  Safe to call on every line -- non-
        matching lines are ignored cheaply.
        """
        if not line:
            return
        stripped = line.strip()
        if stripped.startswith(_PCR_TELEMETRY_PREFIX):
            self._consume_pcr_telemetry(stripped[len(_PCR_TELEMETRY_PREFIX):])
        elif "Peak" in line:
            self._consume_peak_line(line)

    def payload(self) -> dict[str, Any]:
        """Merged telemetry payload ready for the server callback.

        Returns an empty dict if nothing has been parsed -- the caller
        can use that as a signal to skip the network call entirely.
        """
        if not self._in_blender_payload and self._peak_vram_mb is None:
            return {}
        merged = dict(self._in_blender_payload)
        if self._peak_vram_mb is not None:
            merged["peak_vram_mb"] = self._peak_vram_mb
        return merged

    # ── Internal parse helpers ───────────────────────────────────────────

    def _consume_pcr_telemetry(self, json_body: str) -> None:
        try:
            data = json.loads(json_body)
        except Exception:
            return
        if isinstance(data, dict):
            # Last write wins -- typically only one PCR_TELEMETRY line
            # per render but updating idempotently is harmless.
            self._in_blender_payload.update(data)

    def _consume_peak_line(self, line: str) -> None:
        try:
            best = self._peak_vram_mb or 0
            for match in _CYCLES_PEAK_RE.finditer(line):
                val = float(match.group(1))
                unit = match.group(2).upper()
                if unit == "G":
                    val *= 1024
                ival = int(val)
                if ival > best:
                    best = ival
            if best > 0:
                self._peak_vram_mb = best
        except Exception:
            pass
