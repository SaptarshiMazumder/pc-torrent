"""ModalClient — HTTP-only adapter for Modal API.

Split into ModalDispatcher (POST job) and ModalCallIdExtractor (parse response).
No DB, no threads, no business logic.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

import httpx

from serverV2.config import ModalConfig
from serverV2.config.modal.providers.modal_runtime_config_provider import (
    ModalRuntimeConfigProvider,
)

log = logging.getLogger(__name__)
_CALL_ID_RE = re.compile(r"\b(fc-[A-Za-z0-9]+)\b")


class ModalCallIdExtractor:
    """Extracts Modal function_call_id from HTTP responses."""

    @staticmethod
    def extract(response: httpx.Response) -> str:
        candidates: list[str] = []
        for key in (
            "x-modal-function-call-id", "x-function-call-id",
            "x-call-id", "location",
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


class ModalHttpDispatcher:
    """Posts a render job to the Modal web endpoint."""

    def __init__(self, config_provider: ModalRuntimeConfigProvider) -> None:
        self._config_provider = config_provider
        self._extractor = ModalCallIdExtractor()

    def dispatch(
        self,
        gpu_type: str,
        job_id: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_json: str,
        log_streamer_env: dict[str, str],
    ) -> str:
        cfg = self._config_provider.get()
        url = cfg.endpoint_url(gpu_type)
        # Wire-format boundary: the Modal endpoint's input contract takes
        # render_overrides_b64.  Encoding belongs HERE, not upstream — the
        # rest of the server operates on the JSON form.
        render_overrides_b64 = base64.b64encode(
            (render_overrides_json or "{}").encode("utf-8")
        ).decode("ascii")
        payload = {
            "input": {
                "job_id": job_id,
                "blend_url": blend_url,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "frame_step": frame_step,
                "render_overrides_b64": render_overrides_b64,
                "backend_url": cfg.public_backend_url,
                # jobs_logger: PCR_* env pairs.  Modal handler.py stamps
                # them onto os.environ before HandlerLogTap spawns
                # log_streamer.  Empty dict disables capture.
                "log_streamer_env": log_streamer_env,
            }
        }

        log.info("Dispatching job %s to Modal endpoint %s", job_id, gpu_type)
        try:
            resp = httpx.post(
                url,
                headers=_auth_headers(cfg),
                json=payload,
                timeout=cfg.dispatch_timeout_sec,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Modal dispatch timeout for job {job_id} endpoint={gpu_type}"
            ) from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            call_id = self._extractor.extract(resp)
            return call_id if call_id else f"modal-{job_id[:12]}"

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = (exc.response.text or "")[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"Modal dispatch HTTP {exc.response.status_code} for job {job_id} body={body}"
            ) from exc

        modal_job_id = f"modal-{job_id[:12]}"
        header_call_id = self._extractor.extract(resp)
        if header_call_id:
            modal_job_id = header_call_id

        try:
            body = resp.json()
        except Exception:
            body = {}

        function_call_id = str(
            body.get("function_call_id") or body.get("call_id")
            or body.get("provider_job_id") or ""
        ).strip()
        if function_call_id:
            modal_job_id = function_call_id

        return modal_job_id


class ModalClient:
    """Composed facade: dispatch + cancel."""

    def __init__(self, config_provider: ModalRuntimeConfigProvider) -> None:
        self._dispatcher = ModalHttpDispatcher(config_provider)

    def dispatch_job(
        self,
        *,
        job_id: str,
        gpu_type: str,
        blend_url: str,
        frame_start: int,
        frame_end: int,
        frame_step: int,
        render_overrides_json: str,
        log_streamer_env: dict[str, str],
    ) -> str:
        return self._dispatcher.dispatch(
            gpu_type, job_id, blend_url,
            frame_start, frame_end, frame_step,
            render_overrides_json,
            log_streamer_env,
        )

    def cancel_job(self, provider_job_id: str) -> None:
        if not provider_job_id.startswith("fc-"):
            log.warning(
                "Cannot cancel Modal id %s — does not start with 'fc-' "
                "(dispatch likely returned empty body, no function_call_id captured)",
                provider_job_id,
            )
            return
        try:
            import modal
        except Exception as exc:
            log.error("modal import failed during cancel of %s: %s", provider_job_id, exc)
            return
        try:
            modal.functions.FunctionCall.from_id(provider_job_id).cancel()
            log.info("Cancelled Modal call %s", provider_job_id)
        except Exception as exc:
            log.warning(
                "Modal cancel %s raised %s: %s",
                provider_job_id, type(exc).__name__, exc,
            )

    def get_job_status(self, provider_job_id: str) -> dict[str, Any] | None:
        return None


def _auth_headers(cfg: ModalConfig) -> dict[str, str]:
    return {
        "Modal-Key": cfg.token_id,
        "Modal-Secret": cfg.token_secret,
        "Content-Type": "application/json",
    }
