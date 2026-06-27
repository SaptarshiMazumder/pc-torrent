"""VastRuntimeConfigProvider -- assembles a live ``VastConfig`` per call.

Joins the deploy-stable ``VastSecrets`` (env) with the tunable knobs read
FRESH from Firestore via ``RenderConfigRepository`` -- the ``vast`` block,
the ``vast_instances`` list, and ``monitor.in_progress_stale_sec``.

Callers invoke ``get()`` once per logical operation (one search / one
dispatch / one monitor tick), so editing a knob in Firestore (e.g.
``secure_cloud_only``) takes effect on the next operation with no redeploy.
Firestore is the sole source of truth: a missing field fails loud inside
``RenderConfig.from_dict`` -- there is no ``config.json`` fallback.
"""

from __future__ import annotations

from serverV2.config.config import VastConfig, VastEndpoint
from serverV2.config.render_config_repository import RenderConfigRepository
from serverV2.config.vast.vast_secrets import VastSecrets


class VastRuntimeConfigProvider:

    def __init__(
        self,
        *,
        secrets: VastSecrets,
        config_repo: RenderConfigRepository,
    ) -> None:
        self._secrets = secrets
        self._config_repo = config_repo

    def get(self) -> VastConfig:
        rc = self._config_repo.get()
        v = rc.vast
        s = self._secrets
        endpoints = tuple(
            VastEndpoint(
                gpu_name=e.gpu_name,
                label=e.label,
                vram_gb=e.vram_gb,
                cpu_cores=e.cpu_cores,
                ram_gb=e.ram_gb,
                render_speed=e.render_speed,
            )
            for e in rc.vast_instances
        )
        return VastConfig(
            # --- secrets (env, stable) ---
            api_key=s.api_key,
            docker_image=s.docker_image,
            docker_image_eevee=s.docker_image_eevee,
            public_backend_url=s.public_backend_url,
            api_base=s.api_base,
            heartbeat_interval_sec=s.heartbeat_interval_sec,
            # --- knobs (Firestore, live) ---
            provisioning_enabled=v.provisioning_enabled,
            disk_gb=v.disk_gb,
            secure_cloud_only=v.secure_cloud_only,
            poll_interval_sec=v.poll_interval_sec,
            startup_timeout_sec=v.startup_timeout_sec,
            heartbeat_timeout_sec=v.heartbeat_timeout_sec,
            heartbeat_grace_sec=v.heartbeat_grace_sec,
            max_parallel=v.max_parallel,
            in_progress_stale_sec=rc.monitor.in_progress_stale_sec,
            endpoints=endpoints,
        )
