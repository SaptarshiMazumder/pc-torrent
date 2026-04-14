"""StorageClient facade — re-exports all storage operations as a single namespace.

Consumers can do ``from serverV2.infrastructure.storage import file_exists``
or use the composed ``StorageClient`` class.
"""

from serverV2.infrastructure.storage.file_ops import (
    delete_file,
    download_file,
    download_fileobj,
    file_exists,
    get_file_size,
    list_files,
    upload_file,
    upload_fileobj,
)
from serverV2.infrastructure.storage.multipart import (
    abort_multipart_upload,
    complete_multipart_upload,
    create_multipart_upload,
    generate_presigned_upload_part_url,
)
from serverV2.infrastructure.storage.presigner import (
    generate_presigned_upload_url,
    generate_presigned_url,
)

__all__ = [
    "upload_file", "upload_fileobj", "download_file", "download_fileobj",
    "delete_file", "file_exists", "get_file_size", "list_files",
    "generate_presigned_url", "generate_presigned_upload_url",
    "create_multipart_upload", "generate_presigned_upload_part_url",
    "complete_multipart_upload", "abort_multipart_upload",
]
