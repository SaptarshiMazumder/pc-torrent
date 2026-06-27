"""ModalRuntimeConfigProvider assembles a live ModalConfig from env secrets +
Firestore knobs, replicating the boot parser's dispatch-timeout None-collapse
and gpu_type normalize+dedup."""

from __future__ import annotations

from types import SimpleNamespace

from serverV2.config.modal.modal_secrets import ModalSecrets
from serverV2.config.modal.providers.modal_runtime_config_provider import (
    ModalRuntimeConfigProvider,
)


def _instance(gpu_type: str):
    return SimpleNamespace(
        gpu_type=gpu_type, label=f"Modal {gpu_type}",
        vram_gb=48.0, cpu_cores=16, ram_gb=96.0,
        render_speed=1.7, price_per_hour=2.1,
    )


class _StubRepo:
    def __init__(self, *, dispatch_timeout: int = 21600, instances=None) -> None:
        self._rc = SimpleNamespace(
            modal=SimpleNamespace(
                provisioning_enabled=True,
                max_parallel=30,
                per_gpu_max_parallel=5,
                dispatch_timeout_sec=dispatch_timeout,
                in_queue_timeout_sec=120,
                endpoint_url_prefix="",
                availability_sec=14400.0,
            ),
            modal_instances=instances if instances is not None else [_instance("l40s")],
            monitor=SimpleNamespace(in_progress_stale_sec=1800.0),
        )

    def get(self):
        return self._rc


def _secrets() -> ModalSecrets:
    return ModalSecrets(
        token_id="TID", token_secret="TSEC",
        app_name="pcrent-render", workspace="myws",
        public_backend_url="https://backend",
    )


def _provider(**repo_kwargs) -> ModalRuntimeConfigProvider:
    return ModalRuntimeConfigProvider(
        secrets=_secrets(), config_repo=_StubRepo(**repo_kwargs),
    )


def test_assembles_secrets_plus_live_knobs():
    cfg = _provider().get()

    # secrets
    assert cfg.token_id == "TID"
    assert cfg.token_secret == "TSEC"
    assert cfg.app_name == "pcrent-render"
    assert cfg.workspace == "myws"
    assert cfg.public_backend_url == "https://backend"
    assert cfg.heartbeat_interval_sec == 10
    assert cfg.monitor_interval_sec == 30

    # knobs
    assert cfg.provisioning_enabled is True
    assert cfg.dispatch_timeout_sec == 21600.0
    assert cfg.in_queue_timeout_sec == 120
    assert cfg.in_progress_stale_sec == 1800.0
    assert cfg.availability_sec == 14400.0
    assert cfg.max_parallel == 30
    assert cfg.per_gpu_max_parallel == 5
    assert len(cfg.endpoints) == 1
    assert cfg.endpoints[0].gpu_type == "l40s"

    assert cfg.is_enabled() is True


def test_dispatch_timeout_zero_becomes_none():
    # config.py collapsed <= 0 to None ("no timeout"); the provider must too.
    assert _provider(dispatch_timeout=0).get().dispatch_timeout_sec is None


def test_gpu_type_normalized_and_deduped():
    instances = [_instance("L40S"), _instance("l40s"), _instance("H100")]
    cfg = _provider(instances=instances).get()
    assert [e.gpu_type for e in cfg.endpoints] == ["l40s", "h100"]
