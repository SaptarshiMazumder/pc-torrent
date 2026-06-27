"""Unit tests for the affinity module (service + facade).

Mirrors the anti-affinity shape: started/rendered sibling rows -> a
GroupAffinity of preferred (fleet, gpu_type) caps + community machine_ids.
"""

from __future__ import annotations

from serverV2.orchestrator.affinity import (
    AffinityFacade,
    AffinityService,
    GroupAffinity,
)


def test_build_affinity_classifies_rows():
    svc = AffinityService()
    rows = [
        {"machine_type": "vast_serverless", "gpu_type": "RTX 4090", "machine_id": None},
        {"machine_type": "modal_serverless", "gpu_type": "H100", "machine_id": None},
        {"machine_type": "community", "gpu_type": "RTX 3090", "machine_id": "pc-7"},
        # serverless row with no gpu_type contributes nothing
        {"machine_type": "vast_serverless", "gpu_type": "", "machine_id": None},
        # neither serverless gpu nor machine_id -> nothing
        {"machine_type": "", "gpu_type": "", "machine_id": ""},
    ]
    a = svc.build_affinity(rows)
    assert a.preferred_serverless_capabilities == (
        ("modal_serverless", "H100"),
        ("vast_serverless", "RTX 4090"),
    )
    assert a.preferred_machine_ids == ("pc-7",)


def test_build_affinity_dedups():
    svc = AffinityService()
    rows = [
        {"machine_type": "vast_serverless", "gpu_type": "RTX 4090", "machine_id": None},
        {"machine_type": "vast_serverless", "gpu_type": "RTX 4090", "machine_id": None},
        {"machine_type": "community", "gpu_type": "X", "machine_id": "pc-7"},
        {"machine_type": "community", "gpu_type": "X", "machine_id": "pc-7"},
    ]
    a = svc.build_affinity(rows)
    assert a.preferred_serverless_capabilities == (("vast_serverless", "RTX 4090"),)
    assert a.preferred_machine_ids == ("pc-7",)


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    def get_started_siblings(self, group_id):
        return self._rows


def test_facade_composes_repo_and_service():
    facade = AffinityFacade(
        repository=_FakeRepo([
            {"machine_type": "modal_serverless", "gpu_type": "L40S", "machine_id": None},
        ]),
        service=AffinityService(),
    )
    a = facade.affinity_for_group("grp")
    assert a.preferred_serverless_capabilities == (("modal_serverless", "L40S"),)
    assert a.preferred_machine_ids == ()


def test_facade_empty_when_nothing_started():
    facade = AffinityFacade(repository=_FakeRepo([]), service=AffinityService())
    assert facade.affinity_for_group("grp") == GroupAffinity((), ())
