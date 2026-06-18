"""Single public entry point for the LLM module."""
from __future__ import annotations

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse
from serverV2.llm.llm_provider_registry import LLMProviderRegistry


class LLMFacade:
    """Module surface.  Nothing inside ``serverV2/llm/`` leaks beyond
    this class -- callers depend only on (LLMFacade, LLMChatRequest,
    LLMChatResponse, LLMException).
    """

    def __init__(self, registry: LLMProviderRegistry) -> None:
        self._registry = registry

    def chat(
        self,
        provider_name: str,
        request: LLMChatRequest,
    ) -> LLMChatResponse:
        provider = self._registry.get(provider_name)
        return provider.chat(request)
