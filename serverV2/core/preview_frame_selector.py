"""select_preview_frame -- choose the (filename, job_id) to show as a render
group's preview.

The preview should be the latest frame's PRIMARY output (the main render --
``scene.render.filepath``), never a File Output data pass (depth, normal,
crypto, ...).  The worker tags the primary at upload (``output_frames.is_primary``),
so selection is authoritative.  For rows uploaded before the worker started
tagging (is_primary all false), fall back to the worker's naming convention:
File Output passes are named ``<node>_<idx>_frame####.<ext>`` while the main
render is ``[<camera>_]frame####.<ext>`` -- so a filename WITHOUT the
``_<idx>_frame`` marker is the main render.
"""

from __future__ import annotations

import re
from typing import Any

# File Output node pass marker (fallback signal only).  Matches the
# "_<slot-index>_frame####." segment the worker stamps on every File Output
# node file -- the main render never has the numeric slot before "frame".
_PASS_NAME = re.compile(r"_\d+_frame\d+\.", re.IGNORECASE)


def _frame_no(row: dict[str, Any]) -> int:
    n = row.get("frame_number")
    return n if isinstance(n, int) else -1


def _looks_like_pass(filename: str) -> bool:
    return bool(_PASS_NAME.search(filename or ""))


def select_preview_frame(rows: list[dict[str, Any]]) -> tuple[str, str] | None:
    """``rows``: dicts with ``filename``, ``job_id``, ``is_primary``,
    ``frame_number``.  Returns ``(filename, job_id)`` of the latest frame's
    primary render, or ``None`` for an empty set.

    Tier order:
      1. rows tagged ``is_primary`` (authoritative)            -> latest frame
      2. else rows whose name isn't a File Output pass marker  -> latest frame
      3. else the whole set                                    -> latest frame
    """
    if not rows:
        return None
    pool = (
        [r for r in rows if r.get("is_primary")]
        or [r for r in rows if not _looks_like_pass(r.get("filename", ""))]
        or rows
    )
    best = max(pool, key=_frame_no)
    return best.get("filename"), best.get("job_id")
