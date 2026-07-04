"""DaemonStatusRepository — heartbeat write/throttle/read + history + fail-open."""

from __future__ import annotations

from serverV2.services.admin.daemon_status import DaemonStatusRepository


class _FakePipe:
    def __init__(self, store):
        self._store = store

    def hset(self, key, field, value):
        self._store.hashes.setdefault(key, {})[field] = value
        self._store.writes += 1
        return self

    def expire(self, key, ttl):
        return self

    def lpush(self, key, value):
        self._store.lists.setdefault(key, []).insert(0, value)
        return self

    def ltrim(self, key, start, end):
        if key in self._store.lists:
            lst = self._store.lists[key]
            self._store.lists[key] = lst[start:] if end == -1 else lst[start:end + 1]
        return self

    def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.lists = {}
        self.writes = 0

    def pipeline(self, transaction=False):
        return _FakePipe(self)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        return lst[start:] if end == -1 else lst[start:end + 1]


class _FakeRedisClient:
    def __init__(self, fake=None):
        self._fake = fake

    def client(self):
        return self._fake


def _repo():
    fake = _FakeRedis()
    return DaemonStatusRepository(_FakeRedisClient(fake)), fake


def test_report_then_read_roundtrips():
    repo, _ = _repo()
    repo.report("vast_monitor", "inst-1", 7, {"jobs_scanned": 3})
    got = repo.read_all()
    assert got["vast_monitor"]["owner"] == "inst-1"
    assert got["vast_monitor"]["tick_count"] == 7
    assert got["vast_monitor"]["detail"] == {"jobs_scanned": 3}
    assert got["vast_monitor"]["last_tick_ts"] > 0


def test_throttle_skips_rapid_second_write():
    repo, fake = _repo()
    repo.report("vast_monitor", "i", 1, {})
    repo.report("vast_monitor", "i", 2, {})  # within 15s -> throttled
    assert fake.writes == 1


def test_error_report_bypasses_throttle():
    repo, fake = _repo()
    repo.report("vast_monitor", "i", 1, {})
    repo.report("vast_monitor", "i", 2, {}, error="boom")  # error -> always writes
    assert fake.writes == 2
    assert repo.read_all()["vast_monitor"]["error"] == "boom"


def test_history_records_and_reads_newest_first():
    repo, _ = _repo()
    repo.report("vast_monitor", "i", 1, {"jobs_scanned": 3})
    hist = repo.read_history("vast_monitor")
    assert len(hist) == 1
    assert hist[0]["tick_count"] == 1
    assert hist[0]["detail"]["jobs_scanned"] == 3
    assert hist[0]["error"] is False


def test_fail_open_when_redis_down():
    repo = DaemonStatusRepository(_FakeRedisClient(None))
    repo.report("vast_monitor", "i", 1, {})   # no raise
    assert repo.read_all() == {}
    assert repo.read_history("vast_monitor") == []
