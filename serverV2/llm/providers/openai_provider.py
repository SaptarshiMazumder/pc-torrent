"""OpenAI SDK strategy implementation."""
from __future__ import annotations

import json

import openai

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse
from serverV2.llm.llm_exception import LLMException


class OpenAIProvider:
    """Translates ``LLMChatRequest`` -> ``client.chat.completions.create(...)``.

    The canonical request shape is Anthropic-style; this provider
    rewrites it on the way in (``system`` -> system-role message,
    ``tools`` -> OpenAI function-calling shape, ``tool_choice`` ->
    the matching string / object form) and the OpenAI response back
    into Anthropic-style content blocks on the way out.  All
    translation lives here and never leaks to callers.
    """

    name = "openai"

    def __init__(self, api_key: str) -> None:
        self._client = openai.OpenAI(api_key=api_key)

    def chat(self, request: LLMChatRequest) -> LLMChatResponse:
        messages = list(request.messages)
        if request.system is not None:
            messages.insert(
                0, {"role": "system", "content": request.system},
            )

        # ``max_completion_tokens`` is OpenAI's unified parameter
        # (older ``max_tokens`` is rejected by o-series reasoning models
        # like o1 / o3-mini).  gpt-4o family accepts both; use the new
        # name everywhere.
        kwargs: dict = {
            "model": request.model,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
        }
        if request.tools is not None:
            kwargs["tools"] = [
                self._tool_to_openai(tool) for tool in request.tools
            ]
        if request.tool_choice is not None:
            kwargs["tool_choice"] = self._tool_choice_to_openai(
                request.tool_choice,
            )
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            response = self._client.chat.completions.create(**kwargs)
        except openai.APIError as e:
            # ``APIError`` is the documented base of the SDK exception
            # hierarchy -- one catch wraps the whole thing.
            raise LLMException(f"openai API error: {e}") from e

        choice = response.choices[0]
        return LLMChatResponse(
            content_blocks=self._message_to_blocks(choice.message),
            stop_reason=choice.finish_reason or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

    @staticmethod
    def _tool_to_openai(tool: dict) -> dict:
        # Anthropic-style: {name, description, input_schema}
        # OpenAI-style:    {type: "function",
        #                   function: {name, description, parameters}}
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }

    @staticmethod
    def _tool_choice_to_openai(tool_choice: dict):
        kind = tool_choice["type"]
        if kind == "auto":
            return "auto"
        if kind == "any":
            return "required"
        if kind == "tool":
            return {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
        raise LLMException(f"unknown tool_choice type: {kind!r}")

    @staticmethod
    def _message_to_blocks(message) -> list[dict]:
        blocks: list[dict] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls or []:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.function.name,
                    "input": json.loads(call.function.arguments),
                }
            )
        return blocks
