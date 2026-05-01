"""
Modal application for PC Rent GPU rendering.

Each GPU tier (L4 / L40S / H100) gets a stable web endpoint.  The endpoint
spawns a render function that pulls the prebuilt modal worker image,
which has Blender + render scripts + worker_core (shared protocol
library) + modal_worker/ (handler + watchdogs) all baked in.  No
copy_local_dir at deploy time -- the image is reproducible from the
Dockerfile alone.

Deploy:
    pip install modal
    modal setup           # one-time auth
    set MODAL_WORKER_IMAGE_CYCLES=ghcr.io/.../pcrent-modal-worker-cycles:<tag>
    modal deploy modal_worker/app.py

Each function gets a stable URL like:
    https://{workspace}--pcrent-render-render-l4.modal.run
"""

import os

import modal

app = modal.App("pcrent-render")
_WEB_ENDPOINT = modal.fastapi_endpoint if hasattr(modal, "fastapi_endpoint") else modal.web_endpoint

# Cycles image: prebuilt by ./push-worker.sh modal-cycles, pushed to GHCR.
# Pulled by Modal at function-spawn time.  No layers added here -- the
# image is complete.
_CYCLES_IMAGE_REF = os.environ.get("MODAL_WORKER_IMAGE_CYCLES", "").strip()
if not _CYCLES_IMAGE_REF:
    raise RuntimeError(
        "MODAL_WORKER_IMAGE_CYCLES is required for `modal deploy`.\n"
        "  set MODAL_WORKER_IMAGE_CYCLES=ghcr.io/saptarshimazumder/pcrent-modal-worker-cycles:<tag>"
    )
cycles_image = (
    modal.Image.from_registry(_CYCLES_IMAGE_REF, add_python="3.11")
    # worker_core's runtime deps (requests, psutil) need to land under
    # Modal's injected Python 3.11, NOT the base image's python3.10.
    # The requirements file lives in worker_core/ and is also consumed
    # by vast_worker's Dockerfile -- single source of truth.
    .pip_install_from_requirements("worker_core/requirements.txt")
    # fastapi[standard] is Modal-specific (web-endpoint decorator
    # requires it).  Vast/community don't need it.
    .pip_install("fastapi[standard]")
    # Bake the image ref into the container so the runtime re-import of
    # app.py (Modal does this on every function spawn to find function
    # definitions) doesn't trip on the deploy-time env-var-required
    # check above.  Without this the check fires inside every spawned
    # container.
    .env({"MODAL_WORKER_IMAGE_CYCLES": _CYCLES_IMAGE_REF})
)


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

@app.function(image=cycles_image, gpu="L4", timeout=86400, memory=32768, cpu=8, retries=0)
def run_render_l4(input_data: dict):
    return _run_handler(input_data)


@app.function(image=cycles_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_l4(data: dict):
    return _spawn_call(run_render_l4, data)


# ---------------------------------------------------------------------------
# L40S — workhorse tier (48 GB VRAM, Ada arch).  Graphics-optimised data
# centre card, comparable to a 4090 in Cycles performance with double VRAM.
# ---------------------------------------------------------------------------

@app.function(image=cycles_image, gpu="L40S", timeout=86400, memory=98304, cpu=16, retries=0)
def run_render_l40s(input_data: dict):
    return _run_handler(input_data)


@app.function(image=cycles_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_l40s(data: dict):
    return _spawn_call(run_render_l40s, data)


# ---------------------------------------------------------------------------
# H100 80GB — ultra tier (80 GB VRAM, Hopper).  For scenes that need both
# huge VRAM and fast compute — heavy textures + complex geometry combined.
# Faster per-frame than A100 80GB despite being a compute-class card.
# ---------------------------------------------------------------------------

@app.function(image=cycles_image, gpu="H100", timeout=86400, memory=196608, cpu=16, retries=0)
def run_render_h100(input_data: dict):
    return _run_handler(input_data)


@app.function(image=cycles_image, timeout=60, cpu=1)
@_WEB_ENDPOINT(method="POST")
def render_h100(data: dict):
    return _spawn_call(run_render_h100, data)
