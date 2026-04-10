"""
ModalApiClient — HTTP adapter for all Modal API calls.

This is the only module that touches httpx. Business logic never
imports httpx directly.
"""

from __future__ import annotations

import logging
import re

import httpx

from services.modal.config import ModalConfig

log = logging.getLogger(__name__)
_CALL_ID_RE = re.compile(r"\b(fc-[A-Za-z0-9]+)\b")


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
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Modal dispatch timeout for job {job_id} endpoint={gpu_type} "
                f"timeout={self._cfg.dispatch_timeout_sec}"
            ) from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            redirected_id = self._extract_call_id(resp)
            if redirected_id:
                log.warning(
                    "Modal dispatch returned HTTP %s for job %s endpoint=%s; "
                    "using function_call_id from redirect: %s",
                    resp.status_code,
                    job_id,
                    gpu_type,
                    redirected_id,
                )
                return redirected_id
            log.warning(
                "Modal dispatch returned HTTP %s for job %s endpoint=%s without "
                "call id; treating as accepted with synthetic id",
                resp.status_code,
                job_id,
                gpu_type,
            )
            return f"modal-{job_id[:12]}"

        try:
            resp.raise_for_status()
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
        header_call_id = self._extract_call_id(resp)
        if header_call_id:
            modal_job_id = header_call_id

        try:
            body = resp.json()
        except Exception:
            body = {}

        function_call_id = str(
            body.get("function_call_id")
            or body.get("call_id")
            or body.get("provider_job_id")
            or ""
        ).strip()
        if function_call_id:
            modal_job_id = function_call_id
        elif not modal_job_id.startswith("fc-"):
            log.warning(
                f"Modal endpoint {gpu_type} returned no function_call_id for job {job_id}; "
                "falling back to synthetic provider id, cancellation unavailable"
            )

        log.info(f"Dispatched job {job_id} -> Modal endpoint {gpu_type} ({modal_job_id})")
        return modal_job_id

    def _extract_call_id(self, response: httpx.Response) -> str:
        candidates: list[str] = []
        for key in (
            "x-modal-function-call-id",
            "x-function-call-id",
            "x-call-id",
            "location",
        ):
            value = response.headers.get(key)
            if value:
                candidates.append(value)

        try:
            body_text = (response.text or "").strip()
        except Exception:
            body_text = ""
        if body_text:
            candidates.append(body_text)

        for candidate in candidates:
            match = _CALL_ID_RE.search(candidate)
            if match:
                return match.group(1)
        return ""
