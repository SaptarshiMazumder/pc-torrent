"""LLM module -- generic facade + provider strategy.

Public surface (the only thing consumers should import):
  * ``LLMFacade``         -- module entry point
  * ``LLMChatRequest``    -- canonical request value object
  * ``LLMChatResponse``   -- canonical response value object
  * ``LLMException``      -- single error type providers raise

The provider strategies and the registry are internal -- bootstrap
wires them up; consumers go through ``LLMFacade`` (or, on the
allocation side, ``allocation.allocation_llm_client.AllocationLLMClient``).
"""

from serverV2.llm.llm_chat_request import LLMChatRequest
from serverV2.llm.llm_chat_response import LLMChatResponse
from serverV2.llm.llm_exception import LLMException
from serverV2.llm.llm_facade import LLMFacade

__all__ = [
    "LLMChatRequest",
    "LLMChatResponse",
    "LLMException",
    "LLMFacade",
]
