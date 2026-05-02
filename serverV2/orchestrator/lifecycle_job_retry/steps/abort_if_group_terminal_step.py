"""AbortIfGroupTerminalStep — auto-retry pipeline.

Sets ``ctx.aborted = True`` when the group is already cancelled or done,
matching the legacy log line and silent ``return False`` behavior.

# SOURCE: retry_dispatcher.py:84-90 (legacy)
"""

from __future__ import annotations

import logging

from serverV2.orchestrator.lifecycle_job_retry.execution.retry_context import (
    RetryContext,
)

log = logging.getLogger(__name__)


class AbortIfGroupTerminalStep:

    def run(self, ctx: RetryContext) -> None:
        if ctx.aborted:
            return
        grp = ctx.grp
        if grp and grp.get("status") in ("cancelled", "done"):
            log.info(
                "Group %s is %s — not requeuing job %s",
                ctx.group_id, grp["status"], ctx.rj.job_id if ctx.rj else "",
            )
            ctx.aborted = True
