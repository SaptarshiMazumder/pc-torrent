"""Typed configuration for the LLM-backed pre-render cost estimator.

Mirrors the Firestore ``config/cost_estimation`` document.  Kept
separate from ``RenderConfig`` (which mirrors ``config/global``) so
estimator settings can be toggled in isolation without re-writing
the entire allocation config blob.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AllocationCostEstimationConfig:
    """LLM-based pre-render cost estimator settings.

    Read by ``AllocationCostEstimationService.is_enabled`` and
    ``AllocationCostLLMCaller.call`` on every request -- live edits
    in Firestore take effect immediately without a redeploy.

    Feature-flagged off by default -- flip ``cost_estimator_enabled``
    to ``true`` in the ``config/cost_estimation`` doc to roll out.

    Provider + model are also live-tunable so you can A/B different
    OpenAI model versions (or swap to Anthropic) without a redeploy.
    ``provider_name`` must match a name registered in
    ``LLMProviderRegistry`` at boot.
    """
    cost_estimator_enabled: bool
    provider_name: str
    model: str
    # Applied AFTER the LLM formula evaluates -- multiplies the
    # final anchor-GPU seconds figure.  LLMs systematically under-
    # predict Blender render time given limited calibration anchors;
    # this knob covers the gap until anchor coverage improves.
    # Tunable via Firestore so we can dial it down as accuracy
    # improves without a redeploy.
    safety_multiplier: float

    @classmethod
    def defaults(cls) -> "AllocationCostEstimationConfig":
        """Used when the Firestore doc doesn't exist yet -- server
        boots fine, estimator stays off until the doc is created."""
        return cls(
            cost_estimator_enabled=False,
            provider_name="openai",
            model="gpt-4o",
            safety_multiplier=2.0,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AllocationCostEstimationConfig":
        """Per-field backwards-compat: missing fields fall back to
        defaults, so a partial doc (admin only saved a subset of
        fields) still parses."""
        return cls(
            cost_estimator_enabled=bool(d.get("cost_estimator_enabled", False)),
            provider_name=str(d.get("provider_name", "openai")),
            model=str(d.get("model", "gpt-4o")),
            safety_multiplier=float(d.get("safety_multiplier", 2.0)),
        )
