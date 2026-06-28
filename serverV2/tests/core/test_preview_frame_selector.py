"""select_preview_frame -- the group preview is the latest frame's PRIMARY
render (the main image), never a File Output data pass.

Tier order: worker-tagged is_primary > non-pass-named (legacy fallback) >
anything; latest frame within the chosen tier.
"""

from __future__ import annotations

from serverV2.core.preview_frame_selector import select_preview_frame


def _row(filename, job_id, is_primary, frame_number):
    return {
        "filename": filename,
        "job_id": job_id,
        "is_primary": is_primary,
        "frame_number": frame_number,
    }


def test_prefers_tagged_primary_at_the_latest_frame():
    rows = [
        _row("frame0001.png", "j1", True, 1),
        _row("DepthMap_0_frame0001.png", "j1", False, 1),
        _row("frame0002.png", "j2", True, 2),
        _row("DepthMap_0_frame0002.png", "j2", False, 2),
    ]
    assert select_preview_frame(rows) == ("frame0002.png", "j2")


def test_tagged_primary_beats_a_later_frames_data_pass():
    # The latest frame only has its (untagged) pass; the latest TAGGED
    # primary is the right pick, not the later data pass.
    rows = [
        _row("frame0001.png", "j1", True, 1),
        _row("DepthMap_0_frame0009.png", "j1", False, 9),
    ]
    assert select_preview_frame(rows) == ("frame0001.png", "j1")


def test_legacy_fallback_picks_non_pass_name():
    # Nothing tagged (pre-worker-deploy rows): the camera-prefixed main
    # render wins over the File Output pass -- the exact DepthMap bug.
    rows = [
        _row("Camera_frame0005.png", "j1", False, 5),
        _row("DepthMap_0_frame0005.png", "j1", False, 5),
    ]
    assert select_preview_frame(rows) == ("Camera_frame0005.png", "j1")


def test_last_resort_is_latest_frame_when_all_look_like_passes():
    rows = [
        _row("Beauty_0_frame0001.png", "j1", False, 1),
        _row("DepthMap_0_frame0002.png", "j2", False, 2),
    ]
    assert select_preview_frame(rows) == ("DepthMap_0_frame0002.png", "j2")


def test_empty_set_returns_none():
    assert select_preview_frame([]) is None
