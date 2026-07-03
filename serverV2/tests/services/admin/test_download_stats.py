"""DownloadStatsRepository — lifetime + per-day HINCRBY counters, fail-open."""

from __future__ import annotations

from serverV2.services.admin.download_stats import DownloadStatsRepository


class _FakePipe:
    def __init__(self, store):
        self._store = store

    def hincrby(self, key, field, amount):
        h = self._store.setdefault(key, {})
        h[field] = h.get(field, 0) + amount
        return self

    def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.hashes = {}

    def pipeline(self, transaction=False):
        return _FakePipe(self.hashes)

    def hgetall(self, key):
        return {k: str(v) for k, v in self.hashes.get(key, {}).items()}


class _FakeRedisClient:
    def __init__(self, fake=None):
        self._fake = fake

    def client(self):
        return self._fake


def _repo(fake=None):
    return DownloadStatsRepository(_FakeRedisClient(fake if fake is not None else _FakeRedis()))


def test_record_increments_lifetime_and_daily():
    fake = _FakeRedis()
    repo = _repo(fake)
    repo.record("job_zip")
    repo.record("job_zip")
    totals = repo.totals()
    assert totals["job_zip"]["total"] == 2
    assert sum(totals["job_zip"]["by_day"].values()) == 2


def test_totals_empty_when_no_entries():
    assert _repo().totals() == {}


def test_ignores_empty_kind():
    fake = _FakeRedis()
    repo = _repo(fake)
    repo.record("")
    assert repo.totals() == {}


def test_fail_open_when_redis_down():
    repo = DownloadStatsRepository(_FakeRedisClient(None))
    repo.record("agent_windows")   # no-op, no raise
    assert repo.totals() == {}
