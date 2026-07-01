"""Pick the right .blend file to render when the input bundle contains
multiple candidates (zip archives, assets directories).

``find_blend_files`` walks the tree and returns every valid ``.blend``
path (filtering out macOS resource forks and the __MACOSX folder).
``choose_render_target`` ranks them so the result is deterministic and
matches what a user would pick manually -- unless the caller supplied
an explicit ``override_relative_path`` (from the analyze-side dropdown),
in which case that specific file is used.

Ranking preference (heuristic used when no explicit user override):
1. Root-level .blend files only (if any exist).
2. File stem matches the uploaded filename stem.
3. Larger file size.
4. Shorter/lexical relative path.
5. (fallback) Shallower path when no root-level .blend is available.

Shared by every fleet (Modal, Vast, Community) via ``worker_core`` so
a change to the picker logic happens in exactly one place.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def find_blend_files(root_dir: str) -> list[str]:
    """Return all valid ``.blend`` paths under ``root_dir``, sorted."""
    blend_files: list[str] = []
    for current_root, _, files in os.walk(root_dir):
        for name in files:
            lower_name = name.lower()
            if not lower_name.endswith(".blend"):
                continue
            if lower_name.startswith("._"):
                continue
            full_path = os.path.join(current_root, name)
            rel = os.path.relpath(full_path, root_dir).replace("\\", "/")
            if rel.startswith("__MACOSX/") or "/._" in rel:
                continue
            blend_files.append(full_path)
    blend_files.sort()
    return blend_files


def choose_render_target(
    source_filename: str,
    input_dir: str,
    blend_files: list[str],
    override_relative_path: str | None = None,
) -> tuple[str, bool]:
    """Choose a deterministic render target from ``blend_files``.

    When ``override_relative_path`` is provided (user picked a specific
    file from the analyze-side dropdown), we look for that exact
    relative path first.  If it matches a candidate, we return it and
    skip the heuristic ranking below.  If no candidate matches (typo,
    renamed zip, out-of-date override), we log a warning and fall back
    to the heuristic -- the failure mode is IDENTICAL to today's
    behaviour when no override is passed.  Any exception in the
    override branch is swallowed to the same safe fallback so a bug
    here can never break a render.

    Returns ``(chosen_path, selected_from_root)``.  ``selected_from_root``
    is True when at least one root-level .blend exists and we picked
    one, OR when the override matched a specific user pick (the log
    line disambiguates so callers can phrase their messages correctly).
    """
    # ── User override path (only fires when explicitly provided) ─────
    if override_relative_path:
        try:
            for path in blend_files:
                rel = os.path.relpath(path, input_dir).replace("\\", "/")
                if rel == override_relative_path:
                    log.info("[BLEND_SELECT] user override matched: %s", rel)
                    return path, True
            log.warning(
                "[BLEND_SELECT] user override %r not found among %d "
                "candidate(s); falling back to heuristic",
                override_relative_path,
                len(blend_files),
            )
        except Exception as exc:
            log.warning(
                "[BLEND_SELECT] override lookup failed (%s); falling "
                "back to heuristic",
                exc,
            )

    # ── Heuristic (unchanged from pre-override behaviour) ────────────
    source_stem = os.path.splitext(os.path.basename(source_filename))[0].lower()

    entries = []
    for path in blend_files:
        rel = os.path.relpath(path, input_dir).replace("\\", "/")
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        depth = rel.count("/")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        entries.append(
            {
                "path": path,
                "rel": rel,
                "stem_rank": 0 if stem == source_stem else 1,
                "depth": depth,
                "size": size,
            }
        )

    root_entries = [entry for entry in entries if entry["depth"] == 0]
    pool = root_entries if root_entries else entries
    ranked = sorted(
        pool,
        key=lambda entry: (
            entry["stem_rank"],
            -entry["size"],
            entry["depth"],
            len(entry["rel"]),
            entry["rel"].lower(),
        ),
    )
    chosen = ranked[0]
    log.info(
        "[BLEND_SELECT] heuristic pick: %s (from_root=%s, size=%d, depth=%d)",
        chosen["rel"],
        bool(root_entries),
        chosen["size"],
        chosen["depth"],
    )
    return chosen["path"], bool(root_entries)
