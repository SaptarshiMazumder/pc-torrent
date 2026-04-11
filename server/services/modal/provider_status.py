"""Modal function-call status helpers.

Provider polling lives here so the Modal poller can make decisions from real
Modal execution state (not only backend callbacks/progress rows).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Iterable

log = logging.getLogger(__name__)

_SUCCESS_STATUSES = frozenset({"SUCCESS", "DONE", "COMPLETED"})
_FAILURE_STATUSES = frozenset(
    {
        "FAILURE",
        "INIT_FAILURE",
        "TERMINATED",
        "TIMEOUT",
        "CANCELLED",
        "CANCELED",
        "CRASHED",
    }
)
_RUNNING_STATUSES = frozenset({"RUNNING", "ACTIVE", "EXECUTING"})
_PENDING_STATUSES = frozenset(
    {
        "PENDING",
        "QUEUED",
        "SCHEDULED",
        "STARTING",
        "INITIALIZING",
        "LOADING",
        "NOT_STARTED",
    }
)


@dataclass(frozen=True)
class ModalCallSnapshot:
    status: str
    function_name: str | None
    logs: str
    raw_statuses: tuple[str, ...]
    source: str = "call_graph"

    @property
    def is_success(self) -> bool:
        return self.status in _SUCCESS_STATUSES

    @property
    def is_failure(self) -> bool:
        return self.status in _FAILURE_STATUSES

    @property
    def is_running(self) -> bool:
        return self.status in _RUNNING_STATUSES

    @property
    def is_pending(self) -> bool:
        return self.status in _PENDING_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.is_success or self.is_failure


def get_function_call_snapshot(
    function_call_id: str,
    *,
    include_logs: bool = False,
    max_log_lines: int = 200,
) -> ModalCallSnapshot | None:
    function_call_id = (function_call_id or "").strip()
    if not function_call_id or function_call_id.startswith("modal-"):
        return None

    try:
        import modal

        call = modal.FunctionCall.from_id(function_call_id)
        graph = call.get_call_graph()
    except Exception as exc:
        log.warning(
            "Failed to fetch Modal call graph for %s: %s",
            function_call_id,
            exc,
        )
        return None

    root = _root_call(graph)
    if root is None:
        return None

    statuses = [s for s in _collect_statuses(graph) if s]
    root_status = _normalize_status(_node_value(root, "status"))
    status = _aggregate_status(statuses, root_status)
    if not status:
        return None

    logs = ""
    if include_logs:
        logs = _fetch_logs(call, max_log_lines=max_log_lines)

    return ModalCallSnapshot(
        status=status,
        function_name=_node_value(root, "function_name"),
        logs=logs,
        raw_statuses=tuple(statuses),
    )


def get_function_call_status(function_call_id: str) -> ModalCallSnapshot | None:
    """Backward-compatible convenience wrapper."""
    return get_function_call_snapshot(function_call_id, include_logs=False)


def _root_call(graph: Any) -> Any | None:
    if graph is None:
        return None
    if isinstance(graph, (list, tuple)):
        return graph[0] if graph else None
    return graph


def _collect_statuses(graph: Any) -> list[str]:
    values: list[str] = []
    for node in _iter_nodes(graph):
        status = _normalize_status(_node_value(node, "status"))
        if status:
            values.append(status)
    return values


def _iter_nodes(node: Any) -> Iterable[Any]:
    if node is None:
        return

    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_nodes(item)
        return

    if isinstance(node, dict):
        yield node
        children = node.get("children") or node.get("calls") or []
        yield from _iter_nodes(children)
        return

    yield node
    children = getattr(node, "children", None)
    if children:
        yield from _iter_nodes(children)
        return
    calls = getattr(node, "calls", None)
    if calls:
        yield from _iter_nodes(calls)


def _aggregate_status(statuses: list[str], root_status: str) -> str:
    if any(s in _FAILURE_STATUSES for s in statuses):
        for status in statuses:
            if status in _FAILURE_STATUSES:
                return status
    if any(s in _RUNNING_STATUSES for s in statuses):
        return "RUNNING"
    if statuses and all(s in _SUCCESS_STATUSES for s in statuses):
        return "SUCCESS"
    if any(s in _PENDING_STATUSES for s in statuses):
        return "PENDING"
    if root_status:
        return root_status
    return statuses[0] if statuses else ""


def _fetch_logs(call: Any, *, max_log_lines: int) -> str:
    lines: list[str] = []
    try:
        if not hasattr(call, "get_logs"):
            return ""
        stream = call.get_logs()
    except Exception as exc:
        log.debug("Failed to fetch Modal logs: %s", exc)
        return ""

    try:
        if isinstance(stream, str):
            lines = stream.splitlines()
        elif isinstance(stream, bytes):
            lines = stream.decode(errors="replace").splitlines()
        else:
            for row in stream:
                lines.append(_normalize_log_line(row))
    except Exception as exc:
        log.debug("Failed to consume Modal logs stream: %s", exc)
        return ""

    if not lines:
        return ""
    return "\n".join(lines[-max_log_lines:])


def _normalize_log_line(row: Any) -> str:
    if isinstance(row, str):
        return row.rstrip("\n")
    if isinstance(row, dict):
        data = row.get("data")
        if isinstance(data, bytes):
            return data.decode(errors="replace").rstrip("\n")
        if isinstance(data, str):
            return data.rstrip("\n")
        return str(row).rstrip("\n")
    data = getattr(row, "data", None)
    if isinstance(data, bytes):
        return data.decode(errors="replace").rstrip("\n")
    if isinstance(data, str):
        return data.rstrip("\n")
    return str(row).rstrip("\n")


def _normalize_status(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip().upper()
    text = str(value).strip()
    if not text:
        return ""
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.upper()


def _node_value(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        return node.get(key)
    return getattr(node, key, None)
