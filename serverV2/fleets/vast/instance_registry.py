"""Backward-compatible alias — Vast uses the shared InstanceRegistry."""

from serverV2.fleets.instance_registry import InstanceRegistry as VastInstanceRegistry

__all__ = ["VastInstanceRegistry"]
