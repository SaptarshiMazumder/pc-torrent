"""Allocation-side LLM client -- pure pass-through to ``LLMFacade``."""
from __future__ import annotations

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse
from serverV2.llm.llm_facade import LLMFacade


class AllocationLLMClient:
    """Allocation's view into the LLM module.

    Mirrors the ``AllocationClient -> AllocationFacade`` layering
    convention used between the orchestrator and allocation modules.
    Allocation-domain callers depend on this client, not directly on
    ``serverV2.llm.*`` -- so the LLM module's import surface inside
    allocation stays small and swappable (the client can become a
    fake in unit tests without touching downstream code).
    """

    def __init__(self, facade: LLMFacade) -> None:
        self._facade = facade

    def chat(
        self,
        provider_name: str,
        request: LLMChatRequest,
    ) -> LLMChatResponse:
        return self._facade.chat(provider_name, request)
