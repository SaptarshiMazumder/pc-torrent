"""
ModalApiClient — HTTP adapter for all Modal API calls.

This is the only module that touches httpx. Business logic never
imports httpx directly.
"""

from __future__ import annotations

import logging

import httpx

from services.modal.config import ModalConfig

log = logging.getLogger(__name__)


class ModalApiClient:
    def __init__(self, config: ModalConfig) -> None:
        self._cfg = config

    def _headers(self) -> dict[str, str]:
        return {
            "Modal-Key": self._cfg.token_id,
            "Modal-Secret": self._cfg.token_secret,
            "Content-Type": "application/json",
        }

    def dispatch(
        self,
        gpu_type: str,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_b64: str,
    ) -> str:
        """POST a job to the correct Modal web endpoint. Returns a job identifier."""
        url = self._cfg.endpoint_url(gpu_type)
        payload = {
            "input": {
                "job_id": job_id,
                "blend_url": blend_url,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "frame_step": frame_step,
                "render_overrides_b64": render_overrides_b64,
                "backend_url": self._cfg.public_backend_url,
            }
        }

        log.info(f"Dispatching job {job_id} to Modal endpoint {gpu_type} url={url}")
        try:
            resp = httpx.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self._cfg.dispatch_timeout_sec,
            )
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Modal dispatch timeout for job {job_id} endpoint={gpu_type} "
                f"timeout={self._cfg.dispatch_timeout_sec}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = (exc.response.text or "")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"Modal dispatch HTTP {exc.response.status_code} for job {job_id} "
                f"endpoint={gpu_type} body={body}"
            ) from exc

        modal_job_id = f"modal-{job_id[:12]}"
        log.info(f"Dispatched job {job_id} -> Modal endpoint {gpu_type} ({modal_job_id})")
        return modal_job_id
