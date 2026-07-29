"""Presenter: shape RenderGroup domain data into wire DTOs.

Owns everything that used to sit in
``RenderGroupService._build_terminal_status_dto`` and the inline dict
literals in the old router (cost/estimate, pending-queue).  Pure mapping --
no I/O, no business rules.  The presentation layer is the ONLY place a
response shape is decided, so the use-cases can stay wire-format agnostic.
"""

from __future__ import annotations

from typing import Any

from serverV2.core.models import RenderGroup


class RenderGroupPresenter:
    def status_dto(self, group: RenderGroup, *, total_rendered: int) -> dict[str, Any]:
        raise NotImplementedError

    def list_item_dto(self, group: RenderGroup) -> dict[str, Any]:
        raise NotImplementedError
