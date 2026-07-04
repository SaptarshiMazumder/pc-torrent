"""Single public entry point for the LLM module."""
from __future__ import annotations

import logging
from typing import Callable

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse
from serverV2.llm.llm_provider_registry import LLMProviderRegistry

log = logging.getLogger(__name__)


class LLMFacade:
    """Module surface.  Nothing inside ``serverV2/llm/`` leaks beyond
    this class -- callers depend only on (LLMFacade, LLMChatRequest,
    LLMChatResponse, LLMException).

    An optional ``on_usage`` sink is invoked after every successful chat with
    ``(provider_name, model, input_tokens, output_tokens)`` for cost
    observability.  It is called fail-open — a recording error never affects
    the chat result — so the LLM path is unchanged whether or not it's wired.
    """

    def __init__(
        self,
        registry: LLMProviderRegistry,
        on_usage: Callable[[str, str, int, int], None] | None = None,
    ) -> None:
        self._registry = registry
        self._on_usage = on_usage

    def chat(
        self,
        provider_name: str,
        request: LLMChatRequest,
    ) -> LLMChatResponse:
        provider = self._registry.get(provider_name)
        response = provider.chat(request)
        if self._on_usage is not None:
            try:
                self._on_usage(
                    provider_name,
                    request.model,
                    response.input_tokens,
                    response.output_tokens,
                )
            except Exception as exc:  # noqa: BLE001 — observability must not break chat
                log.warning("LLM usage sink failed: %s", exc)
        return response
