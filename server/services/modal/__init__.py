"""
services.modal — public API surface (backward-compatible).

Wires all internal modules together via dependency injection and re-exports
the same top-level names that main.py, modal_strategy.py, and the routers
previously imported from services.modal_dispatch.
"""

from __future__ import annotations

from services.modal.config import ModalConfig
from services.modal.client import ModalApiClient
from services.modal.machine_registrar import MachineRegistrar
from services.modal.dispatcher import ModalDispatcher
from services.modal.monitor import JobMonitor
from services.modal.recovery import StartupRecovery

# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------

_config = ModalConfig.from_env()
_client = ModalApiClient(_config)
_registrar = MachineRegistrar(_config)
_dispatcher = ModalDispatcher(_config, _client, _registrar)

# ---------------------------------------------------------------------------
# Backward-compatible public constants
# ---------------------------------------------------------------------------

PUBLIC_BACKEND_URL: str = _config.public_backend_url
MODAL_WORKERS_PER_ENDPOINT: int = _config.workers_per_endpoint
ENDPOINTS: list[dict] = [
    {"id": ep.gpu_type, "label": ep.label}
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


def start_monitoring_thread(
    job_id: str,
    provider_job_id: str,
    machine_id: str = "",
    blend_url: str = "",
    render_overrides_b64: str = "",
    group_id: str = "",
) -> None:
    monitor = JobMonitor(
        job_id=job_id,
        provider_job_id=provider_job_id,
        machine_id=machine_id,
        blend_url=blend_url,
        render_overrides_b64=render_overrides_b64,
        group_id=group_id,
        config=_config,
    )
    monitor.start()


def cancel_job(provider_job_id: str, machine_id: str = "") -> None:
    _dispatcher.cancel(provider_job_id)


def remove_instance(job_id: str) -> None:
    """Remove a job from the in-memory registry immediately (e.g. on cancel)."""
    from services.modal.instance_registry import registry
    registry.remove(job_id)


def get_instance_states() -> list[dict]:
    from services.modal.instance_registry import registry
    return registry.get_all()


def recover_polling_threads() -> None:
    StartupRecovery(_config).recover()
