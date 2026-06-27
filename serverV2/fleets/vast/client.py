"""VastClient — HTTP-only adapter for the Vast.ai REST API.

Responsibilities: HTTP calls.  No DB, no threads, no business logic.

Config is read live: each public method calls ``config_provider.get()``
once to get a ``VastConfig`` assembled from env secrets + the current
Firestore knobs.  So a Firestore edit (e.g. ``secure_cloud_only``,
``disk_gb``) takes effect on the next call -- no redeploy.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from serverV2.config import VastConfig
from serverV2.config.vast.providers.vast_runtime_config_provider import (
    VastRuntimeConfigProvider,
)
from serverV2.fleets.vast.vast_offer import VastOffer

log = logging.getLogger(__name__)


class VastOfferSearcher:
    """Finds rentable GPU offers on Vast.ai."""

    def __init__(self, config_provider: VastRuntimeConfigProvider) -> None:
        self._config_provider = config_provider

    def search(self, gpu_name: str) -> list[VastOffer]:
        cfg = self._config_provider.get()
        filters: dict = {
            "gpu_name": {"eq": gpu_name},
            "num_gpus": {"eq": 1},
            "rentable": {"eq": True},
            "reliability2": {"gte": 0.90},
            "cuda_max_good": {"gte": 12.3},
            "disk_space": {"gte": cfg.disk_gb},
            "order": [["dph_total", "asc"], ["reliability2", "desc"]],
            "limit": 10,
        }
        if cfg.secure_cloud_only:
            filters["datacenter"] = {"eq": True}

        resp = httpx.get(
            f"{cfg.api_base}/bundles/",
            headers=_auth_headers(cfg),
            params={"q": json.dumps(filters)},
            timeout=30,
        )
        resp.raise_for_status()
        bundles = resp.json().get("offers", [])
        return [VastOffer.from_bundle(b) for b in bundles]


class VastInstanceManager:
    """Creates, inspects, and destroys Vast.ai instances."""

    def __init__(self, config_provider: VastRuntimeConfigProvider) -> None:
        self._config_provider = config_provider

    def create(
        self,
        offer_id: int,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_json: str,
        image: str | None = None,
    ) -> int:
        cfg = self._config_provider.get()
        # Wire-format boundary: the worker container reads the override
        # payload from the RENDER_OVERRIDES_B64 env var.  Base64 keeps
        # the value shell-safe across any docker/runtime quoting layer.
        # Encoding belongs HERE, not upstream — the rest of the server
        # operates on the JSON form.
        render_overrides_b64 = base64.b64encode(
            (render_overrides_json or "{}").encode("utf-8")
        ).decode("ascii")
        env_vars = {
            "JOB_ID": job_id,
            "BLEND_URL": blend_url,
            "FRAME_START": str(frame_start),
            "FRAME_END": str(frame_end),
            "FRAME_STEP": str(frame_step),
            "RENDER_OVERRIDES_B64": render_overrides_b64,
            "BACKEND_URL": cfg.public_backend_url,
        }
        resp = httpx.put(
            f"{cfg.api_base}/asks/{offer_id}/",
            headers=_auth_headers(cfg),
            json={
                "client_id": "me",
                "image": image or cfg.docker_image,
                "env": env_vars,
                "disk": cfg.disk_gb,
                "label": f"pcrent-{job_id[:12]}",
                "runtype": "args",
                "args": ["python3", "-u", "/handler.py"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        instance_id: int = data.get("new_contract") or data.get("id")
        if not instance_id:
            raise RuntimeError(f"Vast.ai create_instance returned no ID: {data}")
        return instance_id

    def get(self, instance_id: int) -> dict[str, Any] | None:
        cfg = self._config_provider.get()
        resp = httpx.get(
            f"{cfg.api_base}/instances/{instance_id}/",
            headers=_auth_headers(cfg),
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data["instances"] if "instances" in data else data

    def list(self) -> list[dict[str, Any]]:
        """Return every instance currently rented on this account.  ONE
        HTTP call replaces N per-job ``get(instance_id)`` calls per
        sweep — the singleton VastFleetMonitor builds an in-memory dict
        keyed by id and looks up each active job's instance from it.
        """
        cfg = self._config_provider.get()
        resp = httpx.get(
            f"{cfg.api_base}/instances/",
            headers=_auth_headers(cfg),
            params={"owner": "me"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("instances") or [])

    def destroy(self, instance_id: int) -> None:
        cfg = self._config_provider.get()
        try:
            resp = httpx.delete(
                f"{cfg.api_base}/instances/{instance_id}/",
                headers=_auth_headers(cfg),
                timeout=30,
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
            log.info("Destroyed Vast.ai instance %s", instance_id)
        except Exception as e:
            log.warning("Failed to destroy Vast.ai instance %s: %s", instance_id, e)

    def get_logs(self, instance_id: int) -> str:
        cfg = self._config_provider.get()
        try:
            resp = httpx.get(
                f"{cfg.api_base}/instances/request_logs/{instance_id}/",
                headers=_auth_headers(cfg),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result") or data.get("logs") or ""
            return ""
        except Exception:
            return ""


class VastClient:
    """Composed facade over offer search + instance management."""

    def __init__(self, config_provider: VastRuntimeConfigProvider) -> None:
        self.offers = VastOfferSearcher(config_provider)
        self.instances = VastInstanceManager(config_provider)

    def dispatch_job(
        self,
        *,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_json: str,
        offer_id: int,
        image: str | None = None,
    ) -> int:
        """Rent a specific offer chosen at planning time, return instance_id."""
        return self.instances.create(
            offer_id=offer_id,
            job_id=job_id,
            blend_url=blend_url,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            render_overrides_json=render_overrides_json,
            image=image,
        )

    def cancel_job(self, instance_id: str) -> None:
        self.instances.destroy(int(instance_id))

    def get_job_status(self, instance_id: str) -> dict[str, Any] | None:
        return self.instances.get(int(instance_id))


def _auth_headers(cfg: VastConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
