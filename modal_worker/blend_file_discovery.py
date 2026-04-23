"""Pick the right .blend file to render when the input bundle contains
multiple candidates (zip archives, assets directories).

``find_blend_files`` walks the tree and returns every valid ``.blend``
path (filtering out macOS resource forks and the __MACOSX folder).
``choose_render_target`` ranks them so the result is deterministic and
matches what a user would pick manually.

Ranking preference:
1. Root-level .blend files only (if any exist).
2. File stem matches the uploaded filename stem.
3. Larger file size.
4. Shorter/lexical relative path.
5. (fallback) Shallower path when no root-level .blend is available.
"""

from __future__ import annotations

import os


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
) -> tuple[str, bool]:
    """Choose a deterministic render target from ``blend_files``.

    Returns ``(chosen_path, selected_from_root)``.  ``selected_from_root``
    is True when at least one root-level .blend exists and we picked one;
    callers use it to decide how to phrase their log line.
    """
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
    return ranked[0]["path"], bool(root_entries)
