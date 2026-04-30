"""Agent-side workflow modules: download, heartbeat helpers.

Mirrors cloud_worker/scripts/workflow/ but smaller -- agent doesn't run
psutil sampling on its own process (the rendering work happens inside
the Docker container, not the agent).  The HeartbeatSender/ProcessSampler
pair from cloud_worker is intentionally absent here; agent.py threads
phase + bytes-progressed into its existing heartbeat payload directly.
"""
