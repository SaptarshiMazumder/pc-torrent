"""
ModalConfig — single source of truth for all Modal configuration.

Reads environment variables once at import time.
Frozen dataclass: immutable, self-documenting, injectable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModalEndpoint:
    gpu_type: str
    label: str


@dataclass(frozen=True)
class ModalConfig:
    token_id: str
    token_secret: str
    app_name: str
    provisioning_enabled: bool
    disabled_gpu_types: frozenset[str]
    gpu_vram_gb: float
    cpu_cores: int
    ram_gb: float
    public_backend_url: str
    workers_per_endpoint: int
    heartbeat_interval_sec: int
    monitor_interval_sec: int
    dispatch_timeout_sec: float | None
    in_queue_timeout_sec: float
    in_progress_stale_sec: float
    endpoint_url_prefix: str
    workspace: str
    endpoints: tuple[ModalEndpoint, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> ModalConfig:
        disabled = _env_csv_set("MODAL_DISABLED_GPU_TYPES", "")
        provisioning_enabled = _env_bool("MODAL_PROVISIONING_ENABLED", True)
        raw_timeout = _env_float("MODAL_DISPATCH_TIMEOUT_SEC", 6 * 60 * 60)

        return cls(
            token_id=os.getenv("MODAL_TOKEN_ID", ""),
            token_secret=os.getenv("MODAL_TOKEN_SECRET", ""),
            app_name=os.getenv("MODAL_APP_NAME", "pcrent-render"),
            provisioning_enabled=provisioning_enabled,
            disabled_gpu_types=frozenset(disabled),
            gpu_vram_gb=_env_float("MODAL_GPU_VRAM_GB", 24.0),
            cpu_cores=_env_int("MODAL_CPU_CORES", 16),
            ram_gb=_env_float("MODAL_RAM_GB", 64.0),
            public_backend_url=os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000"),
            workers_per_endpoint=_env_int("MODAL_WORKERS_PER_ENDPOINT", 3),
            heartbeat_interval_sec=10,
            monitor_interval_sec=30,
            dispatch_timeout_sec=None if raw_timeout <= 0 else raw_timeout,
            in_queue_timeout_sec=_env_float("IN_QUEUE_TIMEOUT_SEC", 120),
            in_progress_stale_sec=_env_float("IN_PROGRESS_STALE_SEC", 90 * 60),
            endpoint_url_prefix=os.getenv("MODAL_ENDPOINT_URL_PREFIX", "").strip().rstrip("/"),
            workspace=os.getenv("MODAL_WORKSPACE", "").strip(),
            endpoints=tuple(_parse_endpoints(disabled)),
        )

    def is_enabled(self) -> bool:
        return bool(
            self.provisioning_enabled
            and self.token_id
            and self.token_secret
            and self.endpoints
        )

    def endpoint_url(self, gpu_type: str) -> str:
        prefix = self.endpoint_url_prefix
        if prefix:
            if "{gpu_type}" in prefix:
                return prefix.format(gpu_type=gpu_type)
            if f"-render-{gpu_type}.modal.run" in prefix:
                return prefix
            if prefix.endswith(".modal.run"):
                return prefix.replace(".modal.run", f"-render-{gpu_type}.modal.run")
            if "--" in prefix:
                return f"{prefix}-render-{gpu_type}.modal.run"
            return f"{prefix}/render-{gpu_type}"
        if self.workspace:
            return (
                f"https://{self.workspace}--{self.app_name}"
                f"-render-{gpu_type}.modal.run"
            )
        raise ValueError(
            "Cannot construct Modal endpoint URL: "
            "set MODAL_ENDPOINT_URL_PREFIX or MODAL_WORKSPACE"
        )


def _parse_endpoints(disabled: set[str]) -> list[ModalEndpoint]:
    raw = os.getenv("MODAL_ENDPOINTS", "").strip()
    if not raw:
        return []

    results: list[ModalEndpoint] = []
    seen_gpu_types: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            gpu_type, label = part.split(":", 1)
            raw_gpu_type = gpu_type.strip()
            gpu_type = _normalize_gpu_type(raw_gpu_type)
            if not gpu_type:
                log.warning(
                    f"Skipping unsupported Modal endpoint gpu_type={raw_gpu_type} "
                    "(only a10/a10g is allowed)"
                )
                continue
            if gpu_type in disabled:
                log.warning(f"Skipping disabled Modal endpoint gpu_type={gpu_type}")
                continue
            if gpu_type in seen_gpu_types:
                log.warning(f"Skipping duplicate Modal endpoint gpu_type={gpu_type}")
                continue
            results.append(ModalEndpoint(gpu_type=gpu_type, label=label.strip()))
            seen_gpu_types.add(gpu_type)
        else:
            raw_gpu_type = part.strip()
            gpu_type = _normalize_gpu_type(raw_gpu_type)
            if not gpu_type:
                log.warning(
                    f"Skipping unsupported Modal endpoint gpu_type={raw_gpu_type} "
                    "(only a10/a10g is allowed)"
                )
                continue
            if gpu_type in disabled:
                log.warning(f"Skipping disabled Modal endpoint gpu_type={gpu_type}")
                continue
            if gpu_type in seen_gpu_types:
                log.warning(f"Skipping duplicate Modal endpoint gpu_type={gpu_type}")
                continue
            results.append(ModalEndpoint(
                gpu_type=gpu_type,
                label=f"Modal {gpu_type.upper()}",
            ))
            seen_gpu_types.add(gpu_type)
    return results


def _normalize_gpu_type(gpu_type: str) -> str | None:
    value = gpu_type.strip().lower()
    if value in {"a10", "a10g"}:
        return "a10g"
    return None


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(f"Invalid {name}='{raw}', using default {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    log.warning(f"Invalid {name}='{raw}', using default {default}")
    return default


def _env_csv_set(name: str, default: str = "") -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return {part.strip().lower() for part in raw.split(",") if part.strip()}
