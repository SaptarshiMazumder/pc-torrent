"""Docker / installer download endpoints.

The community render image used to live in R2 and was served by this
router via ``/docker/image`` + ``/docker/image/version``.  After the
GHCR migration, agents pull directly from
``ghcr.io/saptarshimazumder/pcrent-community-worker`` via
``docker pull`` — same path Vast and Modal use for their images — so
that machinery has been removed.  What's left here:

* ``/docker/version`` — agent self-update version check
* ``/docker/download/{windows,linux}`` — installer artifacts (still in R2)
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from serverV2.infrastructure import storage

router = APIRouter(tags=["docker"])

DOCKER_R2_PREFIX = os.environ.get("DOCKER_R2_PREFIX", "docker-images/")
DOCKER_WINDOWS_KEY = os.environ.get("DOCKER_WINDOWS_KEY", "docker-images/pcrent-agent-windows.zip")
DOCKER_LINUX_KEY = os.environ.get("DOCKER_LINUX_KEY", "docker-images/pcrent-agent-linux.tar.gz")


@router.get("/docker/version")
def docker_version():
    version = os.environ.get("DOCKER_AGENT_VERSION", "0.0.0")
    return {"version": version}


@router.get("/docker/download/windows")
def docker_download_windows():
    if not storage.file_exists(DOCKER_WINDOWS_KEY):
        raise HTTPException(404, "Windows agent not available")
    url = storage.generate_presigned_url(DOCKER_WINDOWS_KEY, download_name="pcrent-agent-windows.zip")
    return RedirectResponse(url)


@router.get("/docker/download/linux")
def docker_download_linux():
    if not storage.file_exists(DOCKER_LINUX_KEY):
        raise HTTPException(404, "Linux agent not available")
    url = storage.generate_presigned_url(DOCKER_LINUX_KEY, download_name="pcrent-agent-linux.tar.gz")
    return RedirectResponse(url)
