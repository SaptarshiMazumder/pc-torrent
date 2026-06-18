"""Provider-agnostic chat response value object."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMChatResponse:
    """Generic chat response.

    ``content_blocks`` are normalised to Anthropic-style dicts:
      * ``{"type": "text", "text": "..."}``
      * ``{"type": "tool_use", "id": "...", "name": "...", "input": {...}}``
      * ``{"type": "thinking", "thinking": "..."}``

    Provider implementations translate their SDK's response shape into
    this normalised form.
    """

    content_blocks: list[dict]
    stop_reason: str
    input_tokens: int
    output_tokens: int

    def text(self) -> str:
        """Concatenation of every text block in order."""
        return "".join(
            block["text"]
            for block in self.content_blocks
            if block["type"] == "text"
        )

    def tool_use(self, name: str) -> dict | None:
        """First tool_use block whose ``name`` matches, or None."""
        for block in self.content_blocks:
            if block["type"] == "tool_use" and block["name"] == name:
                return block
        return None
