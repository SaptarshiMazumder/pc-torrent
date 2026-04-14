"""
services.vast — public API surface (backward-compatible).

Wires all internal modules together via dependency injection and re-exports
the same top-level names that main.py, vast_strategy.py, and the routers
previously imported from services.vast_dispatch.

Consumers can keep doing:
    from services import vast_dispatch   # now resolves to services.vast
    vast_dispatch.register_virtual_machines()
    vast_dispatch.start_heartbeat_thread()
    ...
"""

from __future__ import annotations

from services.vast.config import VastConfig
from services.vast.client import VastApiClient
from services.vast.instance_registry import InstanceRegistry
from services.vast.machine_registrar import MachineRegistrar
from services.vast.dispatcher import VastDispatcher
from services.vast.poller import InstancePoller
from services.vast.recovery import StartupRecovery

# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------

_config = VastConfig.from_env()
_client = VastApiClient(_config)
_registry = InstanceRegistry()
_registrar = MachineRegistrar(_config)
_dispatcher = VastDispatcher(_config, _client, _registrar)
# ---------------------------------------------------------------------------
# Backward-compatible public constants
# ---------------------------------------------------------------------------

PUBLIC_BACKEND_URL: str = _config.public_backend_url
VAST_WORKERS_PER_ENDPOINT: int = _config.workers_per_endpoint
ENDPOINTS: list[dict] = [
    {"id": ep.gpu_name, "label": ep.label, "vram_gb": ep.vram_gb}
    for ep in _config.endpoints
]


# ---------------------------------------------------------------------------
# Backward-compatible public functions
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return _config.is_enabled()


def register_virtual_machines() -> list[str]:
    return _registrar.register_all()


def start_heartbeat_thread() -> None:
    _registrar.start_heartbeat()


def get_instance_states() -> list[dict]:
    return _registry.get_all()


def dispatch_and_save(
    job_id: str,
    blend_url: str,
    frame_start: int,
    frame_end: int,
    frame_step: int,
    render_overrides_b64: str,
    machine_id: str,
) -> str:
    return _dispatcher.dispatch_and_save(
        job_id=job_id,
        blend_url=blend_url,
        frame_start=frame_start,
        frame_end=frame_end,
        frame_step=frame_step,
        render_overrides_b64=render_overrides_b64,
        machine_id=machine_id,
    )


def start_polling_thread(
    job_id: str,
    instance_id: str,
    machine_id: str = "",
    blend_url: str = "",
    render_overrides_b64: str = "",
    group_id: str = "",
) -> None:
    poller = InstancePoller(
        job_id=job_id,
        instance_id=int(instance_id),
        machine_id=machine_id,
        blend_url=blend_url,
        render_overrides_b64=render_overrides_b64,
        group_id=group_id,
        client=_client,
        registry=_registry,
        config=_config,
    )
    poller.start()


def cancel_job(provider_job_id: str, machine_id: str = "") -> None:
    _dispatcher.cancel(provider_job_id)


def remove_instance(job_id: str) -> None:
    """Remove a job from the in-memory registry immediately (e.g. on cancel)."""
    _registry.remove(job_id)


def recover_polling_threads() -> None:
    StartupRecovery(_config, _client, _registry).recover()
