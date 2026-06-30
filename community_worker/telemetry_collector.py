"""TelemetryCollector -- collects per-render observability signals and
emits them as a single ``PCR_TELEMETRY:{json}`` stdout line at the end
of a render.

Phase 11 of the data-richness work.  Pure observation -- this module
NEVER touches anything that affects rendering.  The render driver
imports + calls this AFTER the last frame has been written to disk,
in a try/except so collection failures cannot fail the render.

The collector self-introspects from ``bpy`` -- no state needs to be
plumbed through the render driver.  Callers just do:

    try:
        from telemetry_collector import TelemetryCollector
        TelemetryCollector().emit()
    except Exception as exc:
        log(f"[RENDER_DRIVER] telemetry emit failed (non-fatal): {exc}")

Signals captured (best-effort, each guarded so partial failure never
breaks the whole payload):
    gpu_model_raw         first activated Cycles GPU device name
    device_used           OPTIX / CUDA / HIP / ONEAPI / METAL / CPU / EEVEE
    blender_version       bpy.app.version_string
    denoiser_used         scene.cycles.denoiser (Cycles only) or "none"
    gpu_count             # of activated GPU devices
    cpu_cores             os.cpu_count()
    ram_gb                from /proc/meminfo on Linux
    worker_image_version  from $PCR_WORKER_IMAGE_VERSION

NOT captured here:
    peak_vram_mb          parsed worker-side from Blender's "Peak:N MB"
                          lines -- the in-Blender Python script can't
                          read its own stdout.
    startup_seconds /     server-side parsing of "Saved:" timestamps
    render_seconds        gives a more reliable answer than racing the
                          render_write handler against bpy.ops.render.
"""

from __future__ import annotations

import json
import os
import sys


_CYCLES_DEVICE_TYPE_PRIORITY = ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")


class TelemetryCollector:

    def emit(self) -> None:
        """Build the payload and write a single ``PCR_TELEMETRY:...`` line."""
        gpu_devices = self._gpu_device_names()
        payload = {
            "gpu_model_raw":        gpu_devices[0] if gpu_devices else None,
            "device_used":          self._device_used(),
            "blender_version":      self._blender_version(),
            "denoiser_used":        self._denoiser_used(),
            "gpu_count":            len(gpu_devices) or None,
            "cpu_cores":            self._cpu_cores(),
            "ram_gb":               self._ram_gb(),
            "worker_image_version": os.environ.get("PCR_WORKER_IMAGE_VERSION"),
        }
        # Drop None entries so the worker -> server payload stays small.
        clean = {k: v for k, v in payload.items() if v is not None}
        sys.stdout.write("PCR_TELEMETRY:" + json.dumps(clean, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    # ── Individual signal collectors -- each guarded so partial failure
    # never breaks the whole payload ─────────────────────────────────────

    def _gpu_device_names(self) -> list[str]:
        """Names of currently-activated Cycles GPU devices (``.use=True``,
        non-CPU).  Empty list on EEVEE / CPU-only renders / introspection
        failure -- callers fall back to ``None`` cleanly.
        """
        try:
            import bpy
            cycles_prefs = bpy.context.preferences.addons["cycles"].preferences
            return [
                d.name
                for d in getattr(cycles_prefs, "devices", [])
                if getattr(d, "use", False) and getattr(d, "type", "") != "CPU"
            ]
        except Exception:
            return []

    def _device_used(self) -> str | None:
        """Compute backend label -- ``OPTIX``/``CUDA``/``HIP``/``ONEAPI``/
        ``METAL`` for Cycles GPU, ``CPU`` for Cycles CPU, ``EEVEE`` for
        any EEVEE engine variant.  Returns None on introspection failure.
        """
        try:
            import bpy
            scene = bpy.context.scene
            engine = scene.render.engine
            if engine and engine.startswith("BLENDER_EEVEE"):
                return "EEVEE"
            if engine != "CYCLES":
                return engine  # WORKBENCH or anything custom
            cycles = getattr(scene, "cycles", None)
            if cycles is not None and getattr(cycles, "device", "") == "CPU":
                return "CPU"
            # GPU branch -- find which compute type is actually active.
            cycles_prefs = bpy.context.preferences.addons["cycles"].preferences
            active_types = {
                d.type
                for d in getattr(cycles_prefs, "devices", [])
                if getattr(d, "use", False) and getattr(d, "type", "") != "CPU"
            }
            for preferred in _CYCLES_DEVICE_TYPE_PRIORITY:
                if preferred in active_types:
                    return preferred
            # GPU mode but no recognised compute type -- still useful info.
            return "GPU" if active_types else "CPU"
        except Exception:
            return None

    def _blender_version(self) -> str | None:
        try:
            import bpy
            return bpy.app.version_string
        except Exception:
            return None

    def _denoiser_used(self) -> str | None:
        try:
            import bpy
            scene = bpy.context.scene
            if scene.render.engine != "CYCLES":
                return None
            cycles = getattr(scene, "cycles", None)
            if not cycles or not getattr(cycles, "use_denoising", False):
                return "none"
            return str(getattr(cycles, "denoiser", "") or "").upper() or None
        except Exception:
            return None

    def _cpu_cores(self) -> int | None:
        try:
            return os.cpu_count()
        except Exception:
            return None

    def _ram_gb(self) -> float | None:
        # Linux: parse /proc/meminfo's MemTotal line.  Other platforms
        # return None rather than guessing.
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024 / 1024, 1)
        except Exception:
            pass
        return None
