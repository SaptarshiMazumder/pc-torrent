"""DownloadStatsRepository — counter writes + totals parsing."""

from __future__ import annotations

from serverV2.services.admin.download_stats import DownloadStatsRepository


class _FakePipe:
    def __init__(self, store):
        self._store = store

    def hincrby(self, key, field, amount):
        h = self._store.hashes.setdefault(key, {})
        h[field] = int(h.get(field, 0)) + amount
        return self

    def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, int]] = {}

    def pipeline(self, transaction=False):
        return _FakePipe(self)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


class _FakeRedisClient:
    def __init__(self, fake=None):
        self._fake = fake

    def client(self):
        return self._fake


def test_record_increments_total_and_daily_counters():
    fake = _FakeRedis()
    repo = DownloadStatsRepository(_FakeRedisClient(fake))
    repo.record("job_zip")
    repo.record("job_zip")
    fields = fake.hashes["stats:downloads"]
    assert fields["job_zip"] == 2
    daily = [f for f in fields if f.startswith("job_zip:")]
    assert len(daily) == 1 and fields[daily[0]] == 2


def test_totals_groups_daily_fields_under_their_kind():
    fake = _FakeRedis()
    fake.hashes["stats:downloads"] = {
        "job_zip": 5,
        "job_zip:20260701": 3,
        "job_zip:20260702": 2,
        "agent_windows": 1,
    }
    repo = DownloadStatsRepository(_FakeRedisClient(fake))
    totals = repo.totals()
    assert totals["job_zip"]["total"] == 5
    assert totals["job_zip"]["by_day"] == {"20260701": 3, "20260702": 2}
    assert totals["agent_windows"]["total"] == 1


def test_fail_open_when_redis_down():
    repo = DownloadStatsRepository(_FakeRedisClient(None))
    repo.record("job_zip")   # no raise
    assert repo.totals() == {}
