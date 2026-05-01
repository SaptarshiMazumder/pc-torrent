"""worker_core — shared library for all PC Rent worker fleets.

One canonical implementation of the worker↔server protocol layer:
download with HTTP-range resume, heartbeat sampling + transmission,
incremental output upload, status updates with consistent response
handling across done/failed/cancelled.

Three fleet workers consume this:
    vast_worker    — env-var driven, runs inside Vast.ai container
    modal_worker   — function-call driven, runs as Modal serverless fn
    agent          — Tauri sidecar on user PC (community)

Fleet-specific glue (lifecycle, env handling, Modal SDK shape) stays
in each fleet's own package.  Anything that does NOT need to differ
across fleets lives here.
"""

from worker_core.backend_client import BackendClient
from worker_core.download import BlendDownloader, RangeResumer
from worker_core.heartbeat import (
    BytesProgress,
    HeartbeatSender,
    PhaseTracker,
    ProcessSampler,
)
from worker_core.upload import IncrementalOutputUploader

__all__ = [
    "BackendClient",
    "BlendDownloader",
    "RangeResumer",
    "BytesProgress",
    "HeartbeatSender",
    "PhaseTracker",
    "ProcessSampler",
    "IncrementalOutputUploader",
]
