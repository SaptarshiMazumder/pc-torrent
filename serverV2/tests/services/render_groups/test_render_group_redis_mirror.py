"""RenderGroupRedisMirror — the per-user active-group hash.

Exercises the Redis CRUD against an in-memory fake so no server is needed.
The contract under guard: writes land in the per-user hash, removes clear
them, and reads (single + read_active) round-trip the DTOs.
"""

from __future__ import annotations

from serverV2.services.render_groups.telemetry.render_group_redis_mirror import (
    RenderGroupRedisMirror,
)


class _FakePipe:
    def __init__(self, store):
        self._store = store

    def hset(self, key, field, value):
        self._store.hset(key, field, value)
        return self

    def expire(self, key, ttl):
        return self

    def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self, transaction=False):
        return _FakePipe(self)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


class _FakeRedisClient:
    def __init__(self, fake=None):
        self._fake = fake

    def client(self):
        return self._fake


def _mirror():
    fake = _FakeRedis()
    return RenderGroupRedisMirror(_FakeRedisClient(fake)), fake


def test_write_lands_in_user_hash():
    mirror, fake = _mirror()
    mirror.write("u1", "g1", {"group_id": "g1"})
    assert mirror.read("u1", "g1") == {"group_id": "g1"}
    assert fake.hashes["rgmirror:u1"] == {"g1": '{"group_id": "g1"}'}


def test_remove_clears_entry():
    mirror, _ = _mirror()
    mirror.write("u1", "g1", {"group_id": "g1"})
    mirror.remove("u1", "g1")
    assert mirror.read("u1", "g1") is None


def test_read_active_returns_all_user_groups():
    mirror, _ = _mirror()
    mirror.write("u1", "g1", {"group_id": "g1"})
    mirror.write("u1", "g2", {"group_id": "g2"})
    result = mirror.read_active("u1")
    assert result == {"g1": {"group_id": "g1"}, "g2": {"group_id": "g2"}}


def test_read_active_skips_malformed_entries():
    mirror, fake = _mirror()
    fake.hset("rgmirror:u1", "g1", "not json")
    assert mirror.read_active("u1") == {}


def test_fail_open_when_redis_down():
    mirror = RenderGroupRedisMirror(_FakeRedisClient(None))
    mirror.write("u1", "g1", {"group_id": "g1"})   # no-op, no raise
    mirror.remove("u1", "g1")
    assert mirror.read("u1", "g1") is None
    assert mirror.read_active("u1") == {}
