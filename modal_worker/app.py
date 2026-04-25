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
_WEB_ENDPOINT = modal.fastapi_endpoint if hasattr(modal, "fastapi_endpoint") else modal.web_endpoint
WORKER_IMAGE_REF = os.getenv(
    "MODAL_WORKER_IMAGE",
    "ghcr.io/saptarshimazumder/pcrent-worker:2.08",
).strip()
if not WORKER_IMAGE_REF:
    raise RuntimeError("MODAL_WORKER_IMAGE is empty")

# Reuse the existing GHCR worker image (has Blender 5.0.1, CUDA, render scripts).
# The RunPod 'runpod' pip package is present in the image but harmless — we never
# import it.  We copy in our Modal-specific package that omits runpod imports.
worker_image = (
    modal.Image.from_registry(
        WORKER_IMAGE_REF,
        add_python="3.11",
    ).pip_install(
        "requests",
        "fastapi[standard]",
    )
)

# Copy the whole modal_worker/ package into the image so the handler can
# import its sibling modules (backend_client, modal_heartbeat, watchdogs,
# uploader, blend discovery).  Modal SDK API compatibility:
# - newer SDK: Image.copy_local_dir(...)
# - older SDK: Image.add_local_dir(...)
if hasattr(worker_image, "copy_local_dir"):
    worker_image = worker_image.copy_local_dir(str(_HERE), "/modal_worker")
else:
    worker_image = worker_image.add_local_dir(str(_HERE), "/modal_worker")


def _run_handler(input_data: dict):
    import sys
    if "/" not in sys.path:
        sys.path.insert(0, "/")
    from modal_worker.handler import handler

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
# L4 — entry tier (24 GB VRAM, Ada arch).  Cheap option for small scenes
# that fit comfortably in 24 GB; weaker compute than L40S but a third the
# price.
# ---------------------------------------------------------------------------

@app.function(image=worker_image, gpu="L4", timeout=86400, memory=32768, cpu=8, retries=0)
def run_render_l4(input_data: dict):
    return _run_handler(input_data)


@app.function(image=worker_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_l4(data: dict):
    return _spawn_call(run_render_l4, data)


# ---------------------------------------------------------------------------
# L40S — workhorse tier (48 GB VRAM, Ada arch).  Graphics-optimised data
# centre card, comparable to a 4090 in Cycles performance with double VRAM.
# ---------------------------------------------------------------------------

@app.function(image=worker_image, gpu="L40S", timeout=86400, memory=98304, cpu=16, retries=0)
def run_render_l40s(input_data: dict):
    return _run_handler(input_data)


@app.function(image=worker_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_l40s(data: dict):
    return _spawn_call(run_render_l40s, data)


# ---------------------------------------------------------------------------
# H100 80GB — ultra tier (80 GB VRAM, Hopper).  For scenes that need both
# huge VRAM and fast compute — heavy textures + complex geometry combined.
# Faster per-frame than A100 80GB despite being a compute-class card.
# ---------------------------------------------------------------------------

@app.function(image=worker_image, gpu="H100", timeout=86400, memory=196608, cpu=16, retries=0)
def run_render_h100(input_data: dict):
    return _run_handler(input_data)


@app.function(image=worker_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_h100(data: dict):
    return _spawn_call(run_render_h100, data)
