"""ManualRetryError + the stable reason → HTTP status table.

Lives in the retry package because it's the typed refusal language of
the manual retry pipeline; the API router translates ``reason`` codes to
HTTP statuses but doesn't own the codes themselves.

Single source of truth: any new manual-retry refusal reason is added
here, and ``RETRY_REASON_HTTP_STATUS`` gets the matching status code.
"""

from __future__ import annotations


class ManualRetryError(Exception):
    """User-triggered retry refused.  ``reason`` is a short stable code the
    router maps to an HTTP status — see ``RETRY_REASON_HTTP_STATUS``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


RETRY_REASON_HTTP_STATUS: dict[str, int] = {
    "not_found": 404,
    "not_retryable": 409,
    "active_sibling_exists": 409,
    "group_cancelled": 409,
    "no_remaining_frames": 409,
}
