"""Top-level pytest config for serverV2.

Enforces deterministic hash randomization so that any dict/set iteration
order inside the planner produces identical golden outputs across runs.
``PYTHONHASHSEED`` must be set *before* the Python interpreter starts,
so this module raises if the env var was missing -- the run command
should set it.  Document the canonical invocation:

    PYTHONHASHSEED=0 python -m pytest serverV2/tests
"""

from __future__ import annotations

import os
import sys


_REQUIRED_HASH_SEED = "0"


def pytest_configure(config):  # noqa: ARG001
    seed = os.environ.get("PYTHONHASHSEED")
    if seed != _REQUIRED_HASH_SEED:
        sys.stderr.write(
            f"\n[serverV2/tests] PYTHONHASHSEED must be '{_REQUIRED_HASH_SEED}' for "
            "deterministic golden-file comparisons.\n"
            f"Got: {seed!r}.  Re-run with:\n"
            "    PYTHONHASHSEED=0 python -m pytest serverV2/tests\n\n"
        )
        raise SystemExit(2)
