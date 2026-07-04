"""CostPricingConfig — tunable unit prices for the usage×price estimates.

These are the published pay-as-you-go rates for each provider, used ONLY to
turn usage we can read (commands, GB, instance-hours, tokens) into a dollar
estimate.  They are deliberately editable at runtime via the Firestore
``config/cost_pricing`` doc (see the repository) so you can match your actual
contract without a redeploy — same pattern as ``config/cost_estimation``.

Sources that can bill exactly (GPU render from our own DB; a provider with a
billing key) ignore these entirely.  Defaults are current public list prices
and are intentionally conservative; treat estimates as ballpark until a
billing key upgrades the source to ``exact``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class CostPricingConfig:
    # -- Upstash Redis (pay-as-you-go): ~$0.20 per 100k commands, ~$0.25/GB-mo.
    redis_usd_per_command: float = 0.20 / 100_000
    redis_usd_per_gb_month: float = 0.25
    # -- Neon Postgres: storage ~$0.35/GB-mo.  Compute-hours billed separately
    # and need the Neon API to read exactly; storage is estimable from db size.
    neon_usd_per_gb_month: float = 0.35
    # -- Cloudflare R2: storage $0.015/GB-mo (egress is free on R2).
    r2_usd_per_gb_month: float = 0.015
    # -- Google Cloud Run (region-dependent; asia-northeast1 ballpark):
    # per vCPU-second and per GiB-second, here expressed per-hour.
    cloudrun_usd_per_vcpu_hour: float = 0.0648
    cloudrun_usd_per_gib_hour: float = 0.0072
    # Cloud Run has no usage API here, so we estimate from an assumed always-on
    # shape (edit these to match your service's CPU/mem/min-instances).  Two
    # services run (backend + backup_monitor); default assumes ~1 steady vCPU.
    cloudrun_avg_instances: float = 1.0
    cloudrun_vcpus: float = 1.0
    cloudrun_gib: float = 2.0
    cloudrun_hours_per_month: float = 730.0
    # -- Firestore: $0.06 / 100k reads, $0.18 / 100k writes, $0.02 / 100k deletes.
    firestore_usd_per_read: float = 0.06 / 100_000
    firestore_usd_per_write: float = 0.18 / 100_000
    # -- LLM per-token prices (USD per token).  Phase 3 multiplies recorded
    # input/output token counts by these.  Overridable per model in Firestore
    # under ``llm_prices`` (a nested map); these are sensible fallbacks.
    llm_default_usd_per_input_token: float = 3.0 / 1_000_000
    llm_default_usd_per_output_token: float = 15.0 / 1_000_000

    @classmethod
    def defaults(cls) -> "CostPricingConfig":
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> "CostPricingConfig":
        """Build from a Firestore doc, keeping defaults for any absent /
        malformed field.  Never raises on a bad value — pricing is advisory."""
        known = {f.name for f in fields(cls)}
        overrides: dict[str, float] = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            try:
                overrides[key] = float(value)
            except (TypeError, ValueError):
                continue
        return cls(**overrides)

    def as_dict(self) -> dict:
        return asdict(self)
