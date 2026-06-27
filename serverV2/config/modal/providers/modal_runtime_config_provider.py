"""ModalRuntimeConfigProvider -- assembles a live ``ModalConfig`` per call.

Joins the deploy-stable ``ModalSecrets`` (env) with the tunable knobs read
FRESH from Firestore via ``RenderConfigRepository`` -- the ``modal`` block,
the ``modal_instances`` list, and ``monitor.in_progress_stale_sec``.

Callers invoke ``get()`` once per logical operation (one dispatch / one
monitor tick / one availability build), so editing a knob in Firestore
takes effect on the next operation with no redeploy.  Firestore is the sole
source of truth: a missing field fails loud inside ``RenderConfig.from_dict``
(except ``modal.availability_sec``, which has a back-compat default so docs
that pre-date the field keep parsing).

Two boot-parser behaviours are replicated here so the assembled ModalConfig
is byte-identical to the old one: ``dispatch_timeout_sec <= 0`` collapses to
``None`` (meaning "no timeout"), and Modal gpu_types are normalised +
deduped into the Python-identifier form used as the endpoint-URL suffix.
"""

from __future__ import annotations

from serverV2.config.config import ModalConfig, ModalEndpoint
from serverV2.config.render_config_repository import RenderConfigRepository
from serverV2.config.modal.modal_secrets import ModalSecrets


def _normalize_gpu_type(gpu_type: str) -> str | None:
    """Sanitize a gpu_type into the Python-identifier form usable as a Modal
    function-name suffix (and therefore URL path).  Returns None for
    empty/invalid input.  This is the same sanitization the retired boot
    parser applied to Modal endpoints.
    """
    v = gpu_type.strip().lower().replace("-", "_")
    if not v or not all(c.isalnum() or c == "_" for c in v):
        return None
    return v


class ModalRuntimeConfigProvider:

    def __init__(
        self,
        *,
        secrets: ModalSecrets,
        config_repo: RenderConfigRepository,
    ) -> None:
        self._secrets = secrets
        self._config_repo = config_repo

    def get(self) -> ModalConfig:
        rc = self._config_repo.get()
        m = rc.modal
        s = self._secrets

        endpoints: list[ModalEndpoint] = []
        seen: set[str] = set()
        for e in rc.modal_instances:
            gpu_type = _normalize_gpu_type(e.gpu_type)
            if not gpu_type or gpu_type in seen:
                continue
            seen.add(gpu_type)
            endpoints.append(ModalEndpoint(
                gpu_type=gpu_type,
                label=e.label,
                vram_gb=e.vram_gb,
                cpu_cores=e.cpu_cores,
                ram_gb=e.ram_gb,
                render_speed=e.render_speed,
                price_per_hour=e.price_per_hour,
            ))

        raw_timeout = m.dispatch_timeout_sec
        return ModalConfig(
            # --- secrets (env, stable) ---
            token_id=s.token_id,
            token_secret=s.token_secret,
            app_name=s.app_name,
            workspace=s.workspace,
            public_backend_url=s.public_backend_url,
            heartbeat_interval_sec=s.heartbeat_interval_sec,
            monitor_interval_sec=s.monitor_interval_sec,
            # --- knobs (Firestore, live) ---
            provisioning_enabled=m.provisioning_enabled,
            dispatch_timeout_sec=None if raw_timeout <= 0 else float(raw_timeout),
            in_queue_timeout_sec=m.in_queue_timeout_sec,
            in_progress_stale_sec=rc.monitor.in_progress_stale_sec,
            endpoint_url_prefix=m.endpoint_url_prefix.rstrip("/"),
            availability_sec=m.availability_sec,
            max_parallel=m.max_parallel,
            per_gpu_max_parallel=m.per_gpu_max_parallel,
            endpoints=tuple(endpoints),
        )
