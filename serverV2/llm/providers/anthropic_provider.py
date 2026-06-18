"""Anthropic SDK strategy implementation."""
from __future__ import annotations

import anthropic

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse
from serverV2.llm.llm_exception import LLMException


class AnthropicProvider:
    """Translates ``LLMChatRequest`` -> ``client.messages.create(...)``
    and the SDK response back into ``LLMChatResponse``.
    """

    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def chat(self, request: LLMChatRequest) -> LLMChatResponse:
        kwargs: dict = {
            "model": request.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
        }
        if request.system is not None:
            kwargs["system"] = request.system
        if request.tools is not None:
            kwargs["tools"] = request.tools
        if request.tool_choice is not None:
            kwargs["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            # ``APIError`` is the documented base for every SDK
            # exception (rate-limit, auth, bad-request, timeout,
            # connection, ...).  Mapping the whole hierarchy to a
            # single ``LLMException`` is the point of the facade.
            raise LLMException(f"anthropic API error: {e}") from e

        return LLMChatResponse(
            content_blocks=[
                self._block_to_dict(block) for block in response.content
            ],
            stop_reason=response.stop_reason or "",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    @staticmethod
    def _block_to_dict(block) -> dict:
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        if block.type == "thinking":
            return {"type": "thinking", "thinking": block.thinking}
        # Real upstream boundary: future SDK additions shouldn't crash
        # us.  Pass through the type marker so the caller can decide.
        return {"type": block.type}
