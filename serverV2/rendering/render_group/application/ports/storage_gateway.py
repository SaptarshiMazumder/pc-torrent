"""Port: object storage, narrowed to what rendering actually uses.

The shared kernel's ``IStorageClient`` is broad (multipart, part URLs, ...).
Rendering only needs a handful of operations -- presign the input for
workers, stream outputs into a zip, delete on group teardown.  This
narrower gateway keeps the use-cases honest about their real surface.
"""

from __future__ import annotations

from typing import Protocol


class IStorageGateway(Protocol):
    def presigned_download_url(self, key: str) -> str: ...

    def presigned_upload_url(self, key: str, *, content_type: str) -> str: ...

    def download_bytes(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def delete_prefix(self, prefix: str) -> None: ...
