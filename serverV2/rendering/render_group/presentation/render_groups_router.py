"""Thin controllers for the rendering context.

HTTP in, HTTP out.  Each endpoint decodes the request, calls exactly ONE
use-case, and lets a presenter shape the response -- no orchestration, no
SQL, no DTO-building here.  A factory takes the use-cases (built in
bootstrap) and returns a router; that keeps this module free of globals and
trivially testable.

DORMANT: this router is NOT mounted on the app.  The live endpoints still
live in ``api/routers/render_groups.py``.  Mounting happens only at the
bootstrap wiring step, and only once the use-case bodies are filled in.

Intended endpoint -> use-case map (one call each):
    POST   /render-groups/create                 CreateRenderGroup
    POST   /render-groups/ID/confirm-upload       ConfirmUpload
    POST   /render-groups/ID/cancel               CancelRenderGroup
    DELETE /render-groups/ID                       DeleteRenderGroup
    GET    /render-groups                          ListGroups
    GET    /render-groups/ID / .../status          GetGroupStatus
    GET    /render-groups/ID/outputs               GetOutputs
    GET    /render-groups/ID/cost/estimate         EstimateGroupCost
    POST   /jobs/ID/retry                           RetryHandler
"""

from __future__ import annotations

# Endpoints are added when this context is wired.  Kept import-light and
# unmounted on purpose -- see the DORMANT note above.
