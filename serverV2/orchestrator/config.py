"""Orchestration config — all dispatch/retry/cancel tunables live here.

Reads from config.json's ``orchestrator`` block.  Required field, fails
loud if missing — caps that govern retry budget shouldn't have hidden
defaults.
"""

from __future__ import annotations

from serverV2.config import _require_block, _require_field_int


_block = _require_block("orchestrator")
MAX_RETRIES: int = _require_field_int(_block, "orchestrator", "max_retries")
