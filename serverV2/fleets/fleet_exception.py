"""FleetException — base exception for fleet-level errors.

Concrete fleet errors extend this class as more error semantics emerge
(config validation today; in future, classified dispatch failures,
provisioning errors, etc.).  Caught by callers that need fleet-aware
recovery; uncaught exceptions raised at boot fail loud rather than
silently degrading -- per the project's no-defensive-fallbacks rule.
"""

from __future__ import annotations


class FleetException(Exception):
    """Base exception for all fleet-level errors."""
