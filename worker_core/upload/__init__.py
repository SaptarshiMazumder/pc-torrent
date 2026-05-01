"""worker_core.upload — incremental output uploader.

IncrementalOutputUploader: background thread that scans the render
output directory, uploads ready frames to R2 via presigned URLs, and
registers them with the server.  Frames are uploaded as they become
available so a render that fails mid-way doesn't lose already-rendered
work.
"""

from worker_core.upload.incremental_uploader import IncrementalOutputUploader

__all__ = ["IncrementalOutputUploader"]
