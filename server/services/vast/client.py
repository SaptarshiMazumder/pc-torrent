"""
VastApiClient — HTTP adapter for all Vast.ai API calls.

This is the only module that touches httpx / the Vast.ai REST API.
Business logic never imports httpx directly.
"""

from __future__ import annotations

import json
import logging

import httpx

from services.vast.config import VastConfig

log = logging.getLogger(__name__)


class VastApiClient:
    def __init__(self, config: VastConfig) -> None:
        self._cfg = config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._cfg.api_key}",
            "Content-Type": "application/json",
        }

    def search_offers(self, gpu_name: str) -> list[dict]:
        """Find reliable rentable offers; prefer verified hosts, fall back to all."""
        offers = self._search_query(gpu_name, verified_only=True)
        if offers:
            log.info(f"Vast.ai: found {len(offers)} verified offers for {gpu_name}")
            return offers
        log.info(f"Vast.ai: no verified offers for {gpu_name}, trying unverified")
        return self._search_query(gpu_name, verified_only=False)

    def _search_query(self, gpu_name: str, *, verified_only: bool) -> list[dict]:
        filters: dict = {
            "gpu_name": {"eq": gpu_name},
            "num_gpus": {"eq": 1},
            "rentable": {"eq": True},
            "reliability2": {"gte": 0.90},
            "cuda_max_good": {"gte": 12.0},
            "dph_total": {"lte": self._cfg.max_price_per_gpu},
            "disk_space": {"gte": self._cfg.disk_gb},
            "order": [["dph_total", "asc"], ["reliability2", "desc"]],
            "limit": 10,
        }
        if verified_only:
            filters["verified"] = {"eq": True}

        resp = httpx.get(
            f"{self._cfg.api_base}/bundles/",
            headers=self._headers(),
            params={"q": json.dumps(filters)},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("offers", [])

    def create_instance(
        self,
        offer_id: int,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_b64: str,
    ) -> int:
        """Rent a Vast.ai instance from offer_id and start the render handler."""
        env_vars = {
            "JOB_ID": job_id,
            "BLEND_URL": blend_url,
            "FRAME_START": str(frame_start),
            "FRAME_END": str(frame_end),
            "FRAME_STEP": str(frame_step),
            "RENDER_OVERRIDES_B64": render_overrides_b64,
            "BACKEND_URL": self._cfg.public_backend_url,
            "HEARTBEAT_INTERVAL": str(self._cfg.heartbeat_interval_sec),
        }

        resp = httpx.put(
            f"{self._cfg.api_base}/asks/{offer_id}/",
            headers=self._headers(),
            json={
                "client_id": "me",
                "image": self._cfg.docker_image,
                "env": env_vars,
                "disk": self._cfg.disk_gb,
                "label": f"pcrent-{job_id[:12]}",
                "runtype": "args",
                "args": ["python3", "-u", "/vast_handler.py"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        instance_id: int = data.get("new_contract") or data.get("id")
        if not instance_id:
            raise RuntimeError(f"Vast.ai create_instance returned no ID: {data}")
        return instance_id

    def get_instance(self, instance_id: int) -> dict | None:
        """Fetch a single instance's status from Vast.ai."""
        resp = httpx.get(
            f"{self._cfg.api_base}/instances/{instance_id}/",
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if "instances" in data:
            return data["instances"]
        return data

    def destroy_instance(self, instance_id: int) -> None:
        """Destroy (delete) a Vast.ai instance."""
        try:
            resp = httpx.delete(
                f"{self._cfg.api_base}/instances/{instance_id}/",
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
            log.info(f"Destroyed Vast.ai instance {instance_id}")
        except Exception as e:
            log.warning(f"Failed to destroy Vast.ai instance {instance_id}: {e}")

    def get_logs(self, instance_id: int) -> str:
        """Fetch recent logs from a running Vast.ai instance."""
        try:
            resp = httpx.get(
                f"{self._cfg.api_base}/instances/request_logs/{instance_id}/",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result") or data.get("logs") or ""
            return ""
        except Exception as e:
            log.debug(f"Failed to fetch logs for instance {instance_id}: {e}")
            return ""
