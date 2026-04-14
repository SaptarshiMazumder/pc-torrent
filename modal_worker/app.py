"""
Modal application for PC Rent GPU rendering.

Deploys a single A10G web endpoint. The endpoint receives render jobs via
HTTP POST and executes Blender renders using the same handler logic as the
RunPod worker.

Deploy:
    pip install modal
    modal setup           # one-time auth
    set MODAL_WORKER_IMAGE=ghcr.io/saptarshimazumder/pcrent-worker:<tag>
    modal deploy modal_worker/app.py

Each function gets a stable URL like:
    https://{workspace}--pcrent-render-render-a10g.modal.run
"""

from pathlib import Path
import os

import modal

app = modal.App("pcrent-render")
_HERE = Path(__file__).resolve().parent
_HANDLER_PATH = str(_HERE / "handler.py")
_WEB_ENDPOINT = modal.fastapi_endpoint if hasattr(modal, "fastapi_endpoint") else modal.web_endpoint
WORKER_IMAGE_REF = os.getenv(
    "MODAL_WORKER_IMAGE",
    "ghcr.io/saptarshimazumder/pcrent-worker:2.08",
).strip()
if not WORKER_IMAGE_REF:
    raise RuntimeError("MODAL_WORKER_IMAGE is empty")

# Reuse the existing GHCR worker image (has Blender 5.0.1, CUDA, render scripts).
# The RunPod 'runpod' pip package is present in the image but harmless — we never
# import it.  We copy in our Modal-specific handler that omits runpod imports.
worker_image = (
    modal.Image.from_registry(
        WORKER_IMAGE_REF,
        add_python="3.11",
    ).pip_install(
        "requests",
        "fastapi[standard]",
    )
)

# Modal SDK API compatibility:
# - newer SDK: Image.copy_local_file(...)
# - older SDK: Image.add_local_file(...)
if hasattr(worker_image, "copy_local_file"):
    worker_image = worker_image.copy_local_file(_HANDLER_PATH, "/modal_handler.py")
else:
    worker_image = worker_image.add_local_file(_HANDLER_PATH, "/modal_handler.py")


def _run_handler(input_data: dict):
    import sys
    sys.path.insert(0, "/")
    from modal_handler import handler

    return handler({"input": input_data})


def _spawn_call(fn, data: dict) -> dict:
    input_data = data.get("input", data)
    call = fn.spawn(input_data)
    try:
        call_id = call.object_id
    except Exception:
        call.hydrate()
        call_id = call.object_id
    return {"status": "submitted", "function_call_id": call_id}


# ---------------------------------------------------------------------------
# Single A10G endpoint. Keep backend MODAL_ENDPOINTS aligned to "a10g".
# ---------------------------------------------------------------------------

@app.function(image=worker_image, gpu="A10G", timeout=86400, memory=65536, cpu=16, retries=0)
def run_render_a10g(input_data: dict):
    return _run_handler(input_data)


@app.function(image=worker_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_a10g(data: dict):
    return _spawn_call(run_render_a10g, data)
