"""Provider-agnostic LLM error type."""
from __future__ import annotations


class LLMException(Exception):
    """Single error type the LLM module raises.

    Wraps vendor SDK errors -- timeouts, rate limits, auth failures,
    bad responses, unknown providers.  Each provider maps its native
    SDK exception hierarchy into this so callers only catch one type.
    """
