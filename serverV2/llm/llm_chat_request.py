"""Provider-agnostic chat request value object."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMChatRequest:
    """Generic chat request -- Anthropic's shape is the canonical superset.

    Provider implementations translate this shape to their SDK's native
    request format (e.g. OpenAI moves ``system`` into a leading system
    role message and rewrites ``tools`` into the function-calling shape).
    Callers stay vendor-neutral.
    """

    model: str
    messages: list[dict]
    max_tokens: int
    system: str | None = None
    tools: list[dict] | None = None
    tool_choice: dict | None = None
    temperature: float | None = None
