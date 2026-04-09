from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

import infrastructure.storage as storage

router = APIRouter(tags=["docker"])


@router.get("/docker/image/version")
def get_docker_image_version() -> dict[str, str]:
    try:
        sha_data = storage.download_file("docker/pcrent-render.sha256")
        sha = sha_data.decode().strip().split()[0]
        return {"version": "v1.0.0", "sha256": sha}
    except Exception:
        raise HTTPException(status_code=404, detail="No image available")


@router.get("/docker/image")
def download_docker_image():
    if not storage.file_exists("docker/pcrent-render.tar.gz"):
        raise HTTPException(status_code=404, detail="Image not found")
    url = storage.generate_presigned_url("docker/pcrent-render.tar.gz", expires_in=7200)
    return RedirectResponse(url=url)


@router.get("/docker/linux-image/version")
def get_linux_docker_image_version() -> dict[str, str]:
    try:
        sha_data = storage.download_file("docker/linux/pcrent-render-linux.sha256")
        sha = sha_data.decode().strip().split()[0]
        return {"version": "v1.0.0", "sha256": sha}
    except Exception:
        raise HTTPException(status_code=404, detail="No Linux image available")


@router.get("/docker/linux-image")
def download_linux_docker_image():
    key = "docker/linux/pcrent-render-linux.tar.gz"
    if not storage.file_exists(key):
        raise HTTPException(status_code=404, detail="Linux image not found")
    url = storage.generate_presigned_url(key, expires_in=7200)
    return RedirectResponse(url=url)


@router.get("/releases/latest")
def download_latest_release():
    if not storage.file_exists("releases/PCRentAgent-Setup.exe"):
        raise HTTPException(status_code=404, detail="No release available")
    url = storage.generate_presigned_url("releases/PCRentAgent-Setup.exe", expires_in=7200)
    return RedirectResponse(url=url)
