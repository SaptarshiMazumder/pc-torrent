"""VastSecrets -- the env-sourced, deploy-stable half of the old VastConfig.

API key, worker image tags, the backend callback URL, and the Vast API
base.  These are NOT Firestore-tunable (secrets / per-environment), so they
stay in env vars and are injected once at construction.  The tunable KNOBS
(secure_cloud_only, disk_gb, timeouts, max_parallel, the instance list) come
live from ``RenderConfigRepository`` via ``VastRuntimeConfigProvider``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class VastSecrets:
    api_key: str
    docker_image: str
    docker_image_eevee: str | None
    public_backend_url: str
    # Constant defaults (not Firestore-tunable); kept here so the provider
    # has a single source for every non-knob VastConfig field.
    api_base: str = "https://console.vast.ai/api/v0"
    heartbeat_interval_sec: int = 10

    @classmethod
    def from_env(cls) -> "VastSecrets":
        return cls(
            api_key=os.getenv("VAST_API_KEY", ""),
            docker_image=os.getenv("VAST_DOCKER_IMAGE", ""),
            docker_image_eevee=os.getenv("VAST_DOCKER_IMAGE_EEVEE") or None,
            public_backend_url=os.getenv(
                "PUBLIC_BACKEND_URL", "http://localhost:8000",
            ),
        )
