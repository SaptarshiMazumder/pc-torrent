"""Adapter: ``IStorageGateway`` backed by the shared object-storage client.

Wraps the broad ``IStorageClient`` (S3/R2) down to the narrow surface
rendering needs -- presign input, stream outputs, purge on teardown.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.storage_gateway import IStorageGateway


class StorageGateway(IStorageGateway):
    def __init__(self, storage_client: Any) -> None:
        self._storage_client = storage_client

    def presigned_download_url(self, key: str) -> str:
        raise NotImplementedError

    def presigned_upload_url(self, key: str, *, content_type: str) -> str:
        raise NotImplementedError

    def download_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def list_keys(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> None:
        raise NotImplementedError
