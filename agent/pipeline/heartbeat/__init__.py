"""Heartbeat helpers for the agent.

PhaseTracker  -- thread-safe phase state
BytesProgress -- thread-safe byte counter for download phase

agent.py owns the heartbeat HTTP thread itself; these helpers are
plumbed into its existing payload so the server sees phase +
bytes_progressed alongside the existing rendered_frames signal.
"""

from .bytes_progress import BytesProgress
from .phase_tracker import PhaseTracker
from .sender import JobHeartbeatSender

__all__ = ["BytesProgress", "JobHeartbeatSender", "PhaseTracker"]
