"""Registry holding LLM provider strategies keyed by name."""
from __future__ import annotations

from serverV2.llm.llm_exception import LLMException
from serverV2.llm.llm_provider import LLMProvider


class LLMProviderRegistry:
    """Name-keyed lookup table of providers.  Populated at boot."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> LLMProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise LLMException(
                f"unknown LLM provider: {name!r} "
                f"(registered: {sorted(self._providers)})"
            )
        return provider

    def names(self) -> list[str]:
        return sorted(self._providers)
