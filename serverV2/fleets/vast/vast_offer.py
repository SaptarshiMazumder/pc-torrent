"""VastOffer — typed value object for one rentable Vast.ai bundle.

Replaces the raw ``dict[str, Any]`` shape that ``VastOfferSearcher.search``
used to return.  Carries the per-offer fields the planner needs to score
each offer on its own merits — price, driver version, CUDA version, host
OS — alongside the ``offer_id`` the dispatch path uses to rent THAT exact
offer.

Vast `/bundles/` field semantics (verified live, 2026-05-05):
  * ``os_version`` is a Linux distro version number (e.g. ``"24.04"``).
    There is no explicit OS-family field; Vast hosts are virtually all
    Linux, especially under ``secure_cloud_only=true`` (datacenter-only).
    We normalise to ``"Linux <version>"`` so downstream string matching
    on ``"linux" in host_os.lower()`` works.
  * ``driver_version`` is the NVIDIA driver string (e.g. ``"580.126.09"``).
  * ``cuda_max_good`` is the highest CUDA version the driver supports
    (float, e.g. ``13.0``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VastOffer:
    offer_id: int                # Vast bundle "id" — used in PUT /asks/{offer_id}/
    gpu_name: str                # e.g. "RTX 4090", "A100 SXM4"
    dph_total: float             # marketplace price in USD/hour (this offer)
    cuda_version: str | None     # driver-supported max CUDA, e.g. "13.0"
    driver_version: str | None   # NVIDIA driver string, e.g. "580.126.09"
    host_os: str | None          # normalised OS string, e.g. "Linux 24.04"
    machine_id: int | None       # opaque host id (for anti-affinity at retry)
    reliability: float           # 0..1 — Vast's per-host reliability score

    @classmethod
    def from_bundle(cls, bundle: dict[str, Any]) -> VastOffer:
        cuda_raw = bundle.get("cuda_max_good")
        cuda_version: str | None
        if cuda_raw is None:
            cuda_version = None
        else:
            try:
                cuda_version = f"{float(cuda_raw):.1f}"
            except (TypeError, ValueError):
                cuda_version = None

        driver_raw = bundle.get("driver_version")
        driver_version = str(driver_raw).strip() if driver_raw else None

        # Vast returns os_version as a Linux distro number (e.g. "24.04").
        # Normalise to "Linux <version>" so downstream OS ranking can
        # match "linux" without false negatives on a bare version number.
        os_version_raw = bundle.get("os_version")
        host_os: str | None
        if isinstance(os_version_raw, str) and os_version_raw.strip():
            host_os = f"Linux {os_version_raw.strip()}"
        elif os_version_raw is not None and str(os_version_raw).strip():
            host_os = f"Linux {str(os_version_raw).strip()}"
        else:
            host_os = None

        return cls(
            offer_id=int(bundle["id"]),
            gpu_name=str(bundle.get("gpu_name") or "").strip(),
            dph_total=float(bundle.get("dph_total") or 0.0),
            cuda_version=cuda_version,
            driver_version=driver_version,
            host_os=host_os,
            machine_id=int(bundle["machine_id"]) if bundle.get("machine_id") is not None else None,
            reliability=float(bundle.get("reliability2") or 0.0),
        )
