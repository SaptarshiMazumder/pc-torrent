"""Chunk-progress module — single source of truth for "is this chunk
done?" / "what frames are still missing?"

Public surface is ``ChunkProgressService``; consumers depend only on
it.  ``ChunkProgress`` is the returned value object.
"""

from serverV2.orchestrator.chunk_progress.chunk_progress import ChunkProgress
from serverV2.orchestrator.chunk_progress.chunk_progress_service import (
    ChunkProgressService,
)

__all__ = [
    "ChunkProgress",
    "ChunkProgressService",
]
