"""Adapter: implements rendering's ``IFleetService`` by delegating to the
fleet registry / dispatch layer.

Rendering asks only to tear down a group's in-flight chunks at the fleet
level (on cancel); this adapter routes that to the fleet strategies.
"""

from __future__ import annotations

from typing import Any

from serverV2.rendering.render_group.application.ports.fleet_service import IFleetService


class FleetServiceAdapter(IFleetService):
    def __init__(self, fleet_registry: Any) -> None:
        self._fleet_registry = fleet_registry

    def cancel_all_for_group(self, group_id: str) -> int:
        raise NotImplementedError
