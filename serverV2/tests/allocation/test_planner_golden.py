"""Golden-output regression suite for AllocationPlanner.

Pin-test the planner's output across the Protocol refactor.  Each
scenario serialises ``list[PlannedTask]`` (or ``PlannedTask | None``)
to a stable JSON form and compares against a checked-in golden file.

Workflow:

  1. **First run** -- golden files don't exist yet.  The test creates
     them and skips with a note.  Re-run; from then on the goldens are
     the source of truth.
  2. **Regression** -- normal CI mode.  Outputs must match goldens
     byte-for-byte.  Mismatch = test fails with a unified diff.
  3. **Intentional update** -- delete the affected golden file and
     re-run.  Review the new file before committing.

Hash determinism:

  * Set ``PYTHONHASHSEED=0`` (enforced by ``serverV2/tests/conftest.py``)
  * Each scenario picks distinct ``render_speed`` values, so sort
    tie-breaking can't drift between runs.

Float stability:

  * Python's ``repr(float)`` is round-trip stable since 3.1.  JSON dump
    with ``sort_keys=True`` and integer indent uses ``repr`` for floats,
    so output is byte-stable across CPython 3.10+ on a single machine.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from serverV2.allocation.allocation_strategies.allocation_planner import (
    AllocationPlanner,
)
from serverV2.tests.allocation.planner_scenarios import (
    INITIAL_SCENARIOS,
    RETRY_SCENARIOS,
    StubRegistry,
)


GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Serialisation -- planner output -> stable JSON string
# ---------------------------------------------------------------------------
def _serialise_tasks(tasks) -> str:
    """Render a list of PlannedTask (or a single PlannedTask | None) as
    a deterministic JSON string."""
    if tasks is None:
        payload = None
    elif isinstance(tasks, list):
        payload = [asdict(t) for t in tasks]
    else:
        payload = asdict(tasks)
    return json.dumps(payload, sort_keys=True, indent=2)


def _golden_path(scenario_name: str) -> Path:
    return GOLDEN_DIR / f"{scenario_name}.json"


def _check_or_write(scenario_name: str, actual: str) -> None:
    golden = _golden_path(scenario_name)
    if not golden.exists():
        golden.write_text(actual)
        pytest.skip(
            f"Wrote baseline golden {golden.name}; re-run to switch to regression mode."
        )
    expected = golden.read_text()
    if actual != expected:
        diff_msg = _format_diff(expected, actual)
        pytest.fail(
            f"\nPlanner output diverged from {golden.name}.\n\n{diff_msg}"
        )


def _format_diff(expected: str, actual: str) -> str:
    import difflib
    lines = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile="golden",
        tofile="actual",
        n=3,
    )
    return "".join(lines)


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scenario_name", sorted(INITIAL_SCENARIOS.keys()))
def test_plan_initial_golden(scenario_name: str) -> None:
    inputs = INITIAL_SCENARIOS[scenario_name]()
    planner = AllocationPlanner(registry=StubRegistry())
    tasks = planner.plan_initial(**inputs)
    actual = _serialise_tasks(tasks)
    _check_or_write(scenario_name, actual)


@pytest.mark.parametrize("scenario_name", sorted(RETRY_SCENARIOS.keys()))
def test_plan_retry_golden(scenario_name: str) -> None:
    inputs = RETRY_SCENARIOS[scenario_name]()
    planner = AllocationPlanner(registry=StubRegistry())
    task = planner.plan_retry(**inputs)
    actual = _serialise_tasks(task)
    _check_or_write(scenario_name, actual)
