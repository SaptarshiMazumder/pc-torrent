"""ModalSecrets -- the env-sourced, deploy-stable half of the old ModalConfig.

Modal API tokens, the workspace / app name (which form the endpoint URL),
and the backend callback URL.  These are NOT Firestore-tunable (secrets /
per-environment), so they stay in env vars and are injected once at
construction.  The tunable KNOBS (timeouts, max_parallel, availability,
the instance list) come live from ``RenderConfigRepository`` via
``ModalRuntimeConfigProvider``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModalSecrets:
    token_id: str
    token_secret: str
    app_name: str
    workspace: str
    public_backend_url: str
    # Constant defaults (not Firestore-tunable); kept here so the provider
    # has a single source for every non-knob ModalConfig field.
    heartbeat_interval_sec: int = 10
    monitor_interval_sec: int = 30

    @classmethod
    def from_env(cls) -> "ModalSecrets":
        return cls(
            token_id=os.getenv("MODAL_TOKEN_ID", ""),
            token_secret=os.getenv("MODAL_TOKEN_SECRET", ""),
            app_name=os.getenv("MODAL_APP_NAME", "pcrent-render"),
            workspace=os.getenv("MODAL_WORKSPACE", "").strip(),
            public_backend_url=os.getenv(
                "PUBLIC_BACKEND_URL", "http://localhost:8000",
            ),
        )
