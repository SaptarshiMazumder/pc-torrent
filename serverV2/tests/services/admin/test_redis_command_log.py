"""Redis command logging proxy — records direct + pipelined commands in
memory, adds zero Redis ops, and stays transparent (forwards every call and
returns its result unchanged)."""

from __future__ import annotations

from serverV2.infrastructure.redis_command_log import (
    LoggingRedis,
    RedisActivity,
)


class _FakePipe:
    def __init__(self, store):
        self._store = store

    def hset(self, key, field, value):
        self._store.setdefault(key, {})[field] = value
        return self

    def expire(self, key, ttl):
        return self

    def execute(self):
        return ["ok"]


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value
        return True

    def pipeline(self, transaction=False):
        return _FakePipe(self.store)


def _wrapped():
    activity = RedisActivity(prefix="dev:")
    return LoggingRedis(_FakeRedis(), activity), activity


def test_direct_command_is_recorded_and_forwarded():
    client, activity = _wrapped()
    assert client.set("job:1:hb", "x") is True   # result passes through
    assert client.get("job:1:hb") == "x"
    snap = activity.snapshot()
    assert snap["total"] == 2
    assert snap["by_command"]["set"] == 1
    assert snap["by_command"]["get"] == 1


def test_purpose_is_derived_from_key():
    client, activity = _wrapped()
    client.set("dev:monitor:vast", "1")          # prefixed fixed key
    client.set("job:9:hb", "1")                   # id-keyed heartbeat
    by_purpose = activity.snapshot()["by_purpose"]
    assert by_purpose.get("daemon-lock") == 1
    assert by_purpose.get("worker-heartbeat") == 1


def test_pipeline_counts_each_queued_command_on_execute():
    client, activity = _wrapped()
    pipe = client.pipeline(transaction=False)
    pipe.hset("rgmirror:u1", "g1", "{}")
    pipe.expire("rgmirror:u1", 3600)
    assert activity.snapshot()["total"] == 0      # nothing billed until execute
    pipe.execute()
    snap = activity.snapshot()
    assert snap["total"] == 2
    assert snap["by_command"]["hset"] == 1
    assert snap["by_command"]["expire"] == 1
    assert any(r["source"] == "pipeline" for r in snap["recent"])


def test_reset_clears_counters():
    client, activity = _wrapped()
    client.set("k", "v")
    activity.reset()
    assert activity.snapshot()["total"] == 0
