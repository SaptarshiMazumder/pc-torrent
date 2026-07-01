"""RenderGroupTelemetryService — the mirror decision logic.

Guards the parts that don't touch the DB: refresh writes an ACTIVE group and
removes a TERMINAL one; reads hit the mirror first and only build (Postgres)
on a miss.  ``build_for_group`` is stubbed so these run without a database --
the ~7-query build itself is exercised by the render-groups integration path.
"""

from __future__ import annotations

from serverV2.services.render_groups.telemetry.render_group_telemetry_service import (
    RenderGroupTelemetryService,
)


class _FakeMirror:
    def __init__(self):
        self.store: dict[tuple[str, str], dict] = {}

    def write(self, uid, gid, dto):
        self.store[(uid, gid)] = dto

    def remove(self, uid, gid):
        self.store.pop((uid, gid), None)

    def read(self, uid, gid):
        return self.store.get((uid, gid))

    def read_active(self, uid):
        return {gid: dto for (u, gid), dto in self.store.items() if u == uid}


class _FakeGroupRepo:
    def __init__(self, groups):
        self._groups = groups

    def get_by_id(self, gid):
        return self._groups.get(gid)


def _service(mirror, group_repo, build_calls):
    svc = RenderGroupTelemetryService(
        mirror=mirror,
        group_repo=group_repo,
        job_repo=None,
        machine_repo=None,
        output_frame_repo=None,
        chunk_progress=None,
        scene_resolver=None,
        get_max_retries=lambda: 3,
        get_credits_per_usd=lambda: 100.0,
        actual_cost_compute=None,
    )
    # Stub the ~7-query build so the tests never touch Postgres.
    def _fake_build(group):
        build_calls.append(group["id"])
        return {"group_id": group["id"], "built": True}
    svc.build_for_group = _fake_build  # type: ignore[assignment]
    return svc


def test_refresh_writes_active_group():
    mirror = _FakeMirror()
    repo = _FakeGroupRepo({"g1": {"id": "g1", "user_id": "u1", "status": "running"}})
    calls: list[str] = []
    _service(mirror, repo, calls)._safe_refresh("g1")
    assert mirror.read("u1", "g1") == {"group_id": "g1", "built": True}
    assert calls == ["g1"]


def test_refresh_removes_terminal_group():
    mirror = _FakeMirror()
    mirror.write("u1", "g1", {"stale": True})
    repo = _FakeGroupRepo({"g1": {"id": "g1", "user_id": "u1", "status": "done"}})
    calls: list[str] = []
    _service(mirror, repo, calls)._safe_refresh("g1")
    assert mirror.read("u1", "g1") is None   # terminal -> dropped
    assert calls == []                       # no build for terminal


def test_get_live_returns_mirror_hit_without_building():
    mirror = _FakeMirror()
    mirror.write("u1", "g1", {"group_id": "g1", "cached": True})
    calls: list[str] = []
    svc = _service(mirror, _FakeGroupRepo({}), calls)
    assert svc.get_live("u1", "g1") == {"group_id": "g1", "cached": True}
    assert calls == []                       # hit -> no build


def test_get_live_builds_and_populates_on_miss():
    mirror = _FakeMirror()
    group = {"id": "g1", "user_id": "u1", "status": "running"}
    calls: list[str] = []
    svc = _service(mirror, _FakeGroupRepo({"g1": group}), calls)
    dto = svc.get_live("u1", "g1", group)
    assert dto == {"group_id": "g1", "built": True}
    assert calls == ["g1"]
    assert mirror.read("u1", "g1") == dto     # populated for next read


def test_get_active_map_reads_the_user_hash():
    mirror = _FakeMirror()
    mirror.write("u1", "g1", {"group_id": "g1"})
    mirror.write("u1", "g2", {"group_id": "g2"})
    mirror.write("u2", "g3", {"group_id": "g3"})
    svc = _service(mirror, _FakeGroupRepo({}), [])
    assert set(svc.get_active_map("u1")) == {"g1", "g2"}
