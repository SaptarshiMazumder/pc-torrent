"""VastStatusClassifier — interprets the Vast API's ``actual_status`` +
``status_msg`` pair.

Vast exposes richer container state than Modal — including a string bag of
error fragments that signal daemon-level trouble (OCI runtime errors, CDI
device issues, CUDA errors).  This class is the single place we decide
what those values mean.  Pure logic, no I/O, no decisions about actions.
"""

from __future__ import annotations


_FATAL_MSG_FRAGMENTS = (
    "error response from daemon",
    "oci runtime",
    "failed to create",
    "unresolvable cdi devices",
    "failed to inject",
    "no compatible cycles gpu",
    "cuda error",
    "failed to start container",
)

_BENIGN_STATUSES = frozenset({"running", "exited", "stopped", "offline"})
_EXITED_STATUSES = frozenset({"exited", "stopped", "offline"})


class VastStatusClassifier:

    def has_fatal_error(self, actual_status: str, status_msg: str) -> bool:
        """True when Vast's daemon-level error message appears AND the
        instance is not in a known-benign steady state.
        """
        status = (actual_status or "").lower()
        msg = (status_msg or "").lower()
        if status in _BENIGN_STATUSES:
            return False
        return any(fragment in msg for fragment in _FATAL_MSG_FRAGMENTS)

    def is_running(self, actual_status: str) -> bool:
        return (actual_status or "").lower() == "running"

    def is_exited(self, actual_status: str) -> bool:
        return (actual_status or "").lower() in _EXITED_STATUSES

    def normalize(self, actual_status: str) -> str:
        return (actual_status or "").lower()
