"""Cost accounting — aggregation, fail-open, pricing config, Redis estimate."""

from __future__ import annotations

from serverV2.services.admin.cost import CostAccountingService, CostPricingConfig
from serverV2.services.admin.cost.cost_snapshot import CostSnapshot
from serverV2.services.admin.cost.providers import RedisCostProvider


class _FakeProvider:
    def __init__(self, snap: CostSnapshot) -> None:
        self.name = snap.name
        self._snap = snap

    def snapshot(self) -> CostSnapshot:
        return self._snap


class _BoomProvider:
    name = "boom"

    def snapshot(self) -> CostSnapshot:
        raise RuntimeError("source down")


def test_overview_sums_orders_and_isolates_failures():
    svc = CostAccountingService([
        _FakeProvider(CostSnapshot(name="a", label="A", month_to_date_usd=10.0, live_rate_usd_per_hr=1.0)),
        _FakeProvider(CostSnapshot(name="b", label="B", projected_monthly_usd=5.0, live_rate_usd_per_hr=0.5)),
        _BoomProvider(),
    ])
    ov = svc.overview()
    assert ov["total_month_to_date_usd"] == 15.0          # exact + projected
    assert ov["total_live_rate_usd_per_hr"] == 1.5
    assert [s["name"] for s in ov["sources"]] == ["a", "b", "boom"]  # by spend desc
    boom = next(s for s in ov["sources"] if s["name"] == "boom")
    assert boom["confidence"] == "unavailable"             # one dead source, no crash


def test_source_lookup():
    svc = CostAccountingService([_FakeProvider(CostSnapshot(name="a", label="A"))])
    assert svc.source("a")["label"] == "A"
    assert svc.source("missing") is None


def test_pricing_from_dict_overrides_and_ignores_bad():
    cfg = CostPricingConfig.from_dict({
        "redis_usd_per_gb_month": "0.5",   # coerced
        "neon_usd_per_gb_month": "bad",    # ignored -> default
        "unknown_field": 1,                # ignored
    })
    assert cfg.redis_usd_per_gb_month == 0.5
    assert cfg.neon_usd_per_gb_month == CostPricingConfig.defaults().neon_usd_per_gb_month


class _FakeRedis:
    def info(self, *_a):
        return {
            "instantaneous_ops_per_sec": 10,
            "total_commands_processed": 1000,
            "used_memory": 2_000_000_000,
            "used_memory_human": "2G",
            "connected_clients": 3,
        }

    def dbsize(self):
        return 42


class _FakeRedisClient:
    def __init__(self, fake=None):
        self._fake = fake

    def client(self):
        return self._fake


def test_redis_cost_estimate():
    p = RedisCostProvider(_FakeRedisClient(_FakeRedis()), CostPricingConfig.defaults)
    snap = p.snapshot()
    assert snap.confidence == "estimated"
    assert snap.projected_monthly_usd is not None and snap.projected_monthly_usd > 0
    assert snap.usage["dbsize"] == 42
    assert snap.usage["ops_per_sec"] == 10
    assert len(snap.breakdown) == 2


def test_redis_cost_unavailable_when_down():
    p = RedisCostProvider(_FakeRedisClient(None), CostPricingConfig.defaults)
    assert p.snapshot().confidence == "unavailable"
