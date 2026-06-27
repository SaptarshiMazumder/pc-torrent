"""VastRuntimeConfigProvider assembles a live VastConfig from env secrets +
Firestore knobs, with the secrets fixed and the knobs tracking the repo."""

from __future__ import annotations

from types import SimpleNamespace

from serverV2.config.vast.providers.vast_runtime_config_provider import (
    VastRuntimeConfigProvider,
)
from serverV2.config.vast.vast_secrets import VastSecrets


class _StubRepo:
    """Stands in for RenderConfigRepository -- returns just the slices the
    provider reads (vast block + vast_instances + monitor)."""

    def __init__(self, *, secure_cloud_only: bool) -> None:
        self._rc = SimpleNamespace(
            vast=SimpleNamespace(
                provisioning_enabled=True,
                disk_gb=20.0,
                secure_cloud_only=secure_cloud_only,
                poll_interval_sec=15.0,
                startup_timeout_sec=300.0,
                heartbeat_timeout_sec=45.0,
                heartbeat_grace_sec=90.0,
                max_parallel=30,
            ),
            vast_instances=[
                SimpleNamespace(
                    gpu_name="RTX 4090", label="Vast RTX 4090",
                    vram_gb=24.0, cpu_cores=8, ram_gb=32.0, render_speed=1.7,
                ),
            ],
            monitor=SimpleNamespace(in_progress_stale_sec=1800.0),
        )

    def get(self):
        return self._rc


def _secrets() -> VastSecrets:
    return VastSecrets(
        api_key="KEY",
        docker_image="img:cycles",
        docker_image_eevee="img:eevee",
        public_backend_url="https://backend",
    )


def _provider(secure_cloud_only: bool) -> VastRuntimeConfigProvider:
    return VastRuntimeConfigProvider(
        secrets=_secrets(),
        config_repo=_StubRepo(secure_cloud_only=secure_cloud_only),
    )


def test_assembles_secrets_plus_live_knobs():
    cfg = _provider(secure_cloud_only=True).get()

    # secrets (env, fixed)
    assert cfg.api_key == "KEY"
    assert cfg.docker_image == "img:cycles"
    assert cfg.docker_image_eevee == "img:eevee"
    assert cfg.public_backend_url == "https://backend"
    assert cfg.api_base == "https://console.vast.ai/api/v0"
    assert cfg.heartbeat_interval_sec == 10

    # knobs (Firestore, live)
    assert cfg.secure_cloud_only is True
    assert cfg.disk_gb == 20.0
    assert cfg.max_parallel == 30
    assert cfg.in_progress_stale_sec == 1800.0
    assert len(cfg.endpoints) == 1
    assert cfg.endpoints[0].gpu_name == "RTX 4090"
    assert cfg.endpoints[0].price_per_hour == 0.0

    # helpers still work on the assembled object
    assert cfg.is_enabled() is True
    assert cfg.image_for_engine("BLENDER_EEVEE") == "img:eevee"
    assert cfg.image_for_engine("CYCLES") == "img:cycles"


def test_secure_cloud_only_tracks_the_repo():
    assert _provider(secure_cloud_only=True).get().secure_cloud_only is True
    assert _provider(secure_cloud_only=False).get().secure_cloud_only is False
