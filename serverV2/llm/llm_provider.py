"""LLM provider strategy protocol."""
from __future__ import annotations

from typing import Protocol

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse


class LLMProvider(Protocol):
    """Strategy interface -- one implementation per vendor.

    Adding a new model vendor is one new file under
    ``serverV2/llm/providers/`` plus a single registration line in
    bootstrap.  Existing callers never change.
    """

    name: str

    def chat(self, request: LLMChatRequest) -> LLMChatResponse:
        """Execute one chat turn.

        Raises ``LLMException`` on any failure (auth, network, rate
        limit, bad response, etc.).
        """
        ...
