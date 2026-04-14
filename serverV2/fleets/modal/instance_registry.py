"""Backward-compatible alias — Modal uses the shared InstanceRegistry."""

from serverV2.fleets.instance_registry import InstanceRegistry as ModalInstanceRegistry

__all__ = ["ModalInstanceRegistry"]
