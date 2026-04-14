"""Enumerations shared across serverV2."""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED})


class MachineType(str, Enum):
    VAST_SERVERLESS = "vast_serverless"
    MODAL_SERVERLESS = "modal_serverless"
    WINDOWS = "windows"

    @property
    def is_serverless(self) -> bool:
        return self in _SERVERLESS_TYPES


_SERVERLESS_TYPES = frozenset({MachineType.VAST_SERVERLESS, MachineType.MODAL_SERVERLESS})

SERVERLESS_TYPE_VALUES: frozenset[str] = frozenset({t.value for t in _SERVERLESS_TYPES})


class CallbackOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PROGRESS = "progress"


class GroupStatus(str, Enum):
    UPLOADING = "uploading"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_GROUP_STATUSES


_TERMINAL_GROUP_STATUSES = frozenset({GroupStatus.DONE, GroupStatus.FAILED, GroupStatus.CANCELLED})
