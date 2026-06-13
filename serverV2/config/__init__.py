"""serverV2.config -- centralised configuration package.

``config.py`` holds the typed dataclasses and JSON loaders.  ``credits.py``
holds pure USD <-> credits math.  Both are re-exported here so callers
can keep ``from serverV2.config import AppConfig`` and friends.
"""

from serverV2.config.config import (
    AppConfig,
    BillingConfig,
    EngineFactors,
    FailureRateConfig,
    FrameAllocationConfig,
    ModalConfig,
    ModalEndpoint,
    RenderStartupSec,
    RenderTimeConfig,
    SceneScalingConfig,
    StallDetectionConfig,
    StartupBufferConfig,
    VastConfig,
    VastEndpoint,
    VramFleetBoostConfig,
)
from serverV2.config.credits import usd_to_credits

__all__ = [
    "AppConfig",
    "BillingConfig",
    "EngineFactors",
    "FailureRateConfig",
    "FrameAllocationConfig",
    "ModalConfig",
    "ModalEndpoint",
    "RenderStartupSec",
    "RenderTimeConfig",
    "SceneScalingConfig",
    "StallDetectionConfig",
    "StartupBufferConfig",
    "VastConfig",
    "VastEndpoint",
    "VramFleetBoostConfig",
    "usd_to_credits",
]
