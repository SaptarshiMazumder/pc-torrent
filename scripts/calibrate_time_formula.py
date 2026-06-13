"""Offline calibration of the per-frame time formula from render_telemetry.

Pulls every completed chunk, reconstructs what the current formula PREDICTED
for each, and fits better coefficients via log-space linear regression.
Prints a proposed Firestore config diff at the end.

Read-only: no Firestore writes, no Neon writes. The script prints; you decide.

Usage:
    python -m scripts.calibrate_time_formula

Env required:
    DATABASE_URL                Neon connection string
    GOOGLE_APPLICATION_CREDENTIALS  Firebase service-account JSON

Model:
    log(actual_spf) + log(current_render_speed) - log(scene_factor)
      = log(baseline_engine) + Σ flag * log(feature_multiplier) + ε

    scene_factor (sample × pixel × geometry) stays FIXED -- those curves
    aren't being recalibrated here; they're scene-size scaling and look fine.

    Per-machine render_speed correction is computed as a second pass:
    mean(residual) per machine -> proposed render_speed = current * exp(-mean_residual).

    Coefficients with fewer than CONFIDENCE_FLOOR supporting rows are
    dropped from the patch -- keep the current value.

Output:
    - Coefficient table with supporting-row counts.
    - Per-machine residual table (current vs proposed render_speed).
    - JSON patch suitable for pasting into ConfigurationPage.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections import defaultdict
from typing import Any

import numpy as np

from serverV2.allocation.render_config import RenderConfig
from serverV2.core.value_objects import parse_analysis_heaviness
from serverV2.infrastructure.auth.firebase_app import init_firebase
from serverV2.infrastructure.db import query_all


CONFIDENCE_FLOOR = 10
EEVEE_ENGINES = frozenset({"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"})
BASELINE_VERTS = 100_000
MIN_GEOMETRY_FACTOR = 0.7


log = logging.getLogger("calibrate_time_formula")


def fetch_firestore_config() -> RenderConfig:
    from firebase_admin import firestore
    init_firebase()
    client = firestore.client()
    doc = client.collection("config").document("global").get()
    if not doc.exists:
        raise SystemExit("Firestore config/global doc not found")
    raw = (doc.to_dict() or {}).get("json")
    if not raw:
        raise SystemExit("Firestore config/global has no 'json' field")
    return RenderConfig.from_dict(json.loads(raw))


def render_speed_lookup(cfg: RenderConfig) -> dict[tuple[str, str], float]:
    """(fleet, gpu_key_lower) -> render_speed. Fleet matches telemetry's
    fleet column verbatim (``vast_serverless`` / ``modal_serverless``).
    gpu_key is lowercased so a label/gpu_name/gpu_type mismatch in case
    still resolves."""
    speeds: dict[tuple[str, str], float] = {}
    for v in cfg.vast_instances:
        for k in (v.label, v.gpu_name):
            speeds[("vast_serverless", (k or "").strip().lower())] = v.render_speed
    for m in cfg.modal_instances:
        for k in (m.label, m.gpu_type):
            speeds[("modal_serverless", (k or "").strip().lower())] = m.render_speed
    return speeds


def sample_factor(samples: int, scaling) -> float:
    if samples <= 0 or scaling.baseline_samples <= 0:
        return 1.0
    ratio = samples / scaling.baseline_samples
    return max(scaling.min_sample_factor, ratio ** scaling.sample_curve_exponent)


def pixel_factor(pixels: int, scaling) -> float:
    if pixels <= 0 or scaling.baseline_pixels <= 0:
        return 1.0
    ratio = pixels / scaling.baseline_pixels
    return max(scaling.min_pixel_factor, ratio ** scaling.pixel_curve_exponent)


def geometry_factor(verts: int) -> float:
    if verts <= 0:
        return 1.0
    ratio = verts / BASELINE_VERTS
    return max(MIN_GEOMETRY_FACTOR, 1.0 + 0.5 * math.log10(max(0.1, ratio)))


FEATURE_COLS = [
    # (obs_key,        json_field,                          patch_path)
    ("is_cycles",      "baseline_sec_cycles",               ["baseline_sec_cycles"]),
    ("is_eevee",       "baseline_sec_eevee",                ["baseline_sec_eevee"]),
    ("is_sub",         "factors_cycles.subdivision",        ["factors_cycles", "subdivision"]),
    ("is_disp",        "factors_cycles.displacement",       ["factors_cycles", "displacement"]),
    ("is_particles",   "factors_cycles.particles",          ["factors_cycles", "particles"]),
    ("is_sss",         "factors_cycles.subsurface",         ["factors_cycles", "subsurface"]),
    ("is_vol",         "factors_cycles.volumetrics",        ["factors_cycles", "volumetrics"]),
    ("is_gn",          "(geometry_nodes — manual map)",     None),
    ("is_adapt",       "factors_cycles.adaptive_sampling",  ["factors_cycles", "adaptive_sampling"]),
]


def build_observations(rows: list[dict], cfg: RenderConfig) -> tuple[list[dict], dict[str, int]]:
    rt = cfg.frame_allocation.render_time
    speeds = render_speed_lookup(cfg)
    obs: list[dict] = []
    skipped = {"unknown_machine": 0, "zero_time": 0, "missing_heaviness": 0}

    for r in rows:
        if not r["seconds_total"]:
            skipped["zero_time"] += 1
            continue
        frames = r["rendered_frames"] or r["chunk_size"]
        if not frames or frames <= 0:
            skipped["zero_time"] += 1
            continue
        actual_spf = float(r["seconds_total"]) / float(frames)
        if actual_spf <= 0:
            skipped["zero_time"] += 1
            continue

        raw_h = r.get("heaviness_json") or {}
        if not raw_h:
            skipped["missing_heaviness"] += 1
            continue
        h = parse_analysis_heaviness({"heaviness": raw_h})

        engine = str(h.get("render_engine") or "")
        is_eevee = engine in EEVEE_ENGINES
        scaling = rt.scene_scaling_eevee if is_eevee else rt.scene_scaling_cycles

        gpu_key = ((r["fleet"] or "").strip(),
                   (r["gpu_type"] or "").strip().lower())
        speed = speeds.get(gpu_key)
        if speed is None or speed <= 0:
            skipped["unknown_machine"] += 1
            continue

        sf = sample_factor(int(h["samples"]), scaling)
        pf = pixel_factor(int(h["effective_pixels"]), scaling)
        gf = geometry_factor(int(h["vertex_count_total"]))
        scene = sf * pf * gf
        if scene <= 0:
            skipped["zero_time"] += 1
            continue

        # target = log(baseline_engine) + Σ flag*log(mult)  [after moving knowns to LHS]
        target = math.log(actual_spf) + math.log(speed) - math.log(scene)

        obs.append({
            "row_id": r["id"],
            "machine": gpu_key,
            "is_cycles": (not is_eevee) and engine != "",
            "is_eevee": is_eevee,
            "is_sub": bool(h["uses_subdivision"]),
            "is_disp": bool(h["uses_displacement"]),
            "is_particles": bool(h["uses_particles"]),
            "is_sss": bool(h["uses_subsurface_scattering"]),
            "is_vol": bool(h["uses_volumetrics"]),
            "is_gn": bool(h["uses_geometry_nodes"]),
            "is_adapt": bool(h["uses_adaptive_sampling"]),
            "target": target,
            "actual_spf": actual_spf,
            "current_speed": speed,
        })

    # Treat unlabelled-engine rows as cycles for the intercept (their is_cycles
    # is false above so they would have no engine indicator).  Fall back rather
    # than dropping -- most legacy rows lack the engine string.
    for o in obs:
        if not o["is_cycles"] and not o["is_eevee"]:
            o["is_cycles"] = True

    return obs, skipped


def fit(obs: list[dict]) -> tuple[dict[str, float], np.ndarray]:
    """Returns (coefficient by feature key, residual array)."""
    keys = [k for k, _, _ in FEATURE_COLS]
    X = np.array([[1.0 if o[k] else 0.0 for k in keys] for o in obs])
    y = np.array([o["target"] for o in obs])

    # Drop columns with zero support to avoid a singular system.
    col_sums = X.sum(axis=0)
    keep_idx = [i for i, s in enumerate(col_sums) if s > 0]
    drop_keys = [keys[i] for i in range(len(keys)) if i not in keep_idx]
    X_keep = X[:, keep_idx]

    coefs_keep, *_ = np.linalg.lstsq(X_keep, y, rcond=None)

    coefs: dict[str, float] = {k: 0.0 for k in keys}
    for fitted_idx, original_idx in enumerate(keep_idx):
        coefs[keys[original_idx]] = float(coefs_keep[fitted_idx])

    y_pred = X_keep @ coefs_keep
    residual = y - y_pred
    if drop_keys:
        log.info("(skipped zero-support coefficients: %s)", ", ".join(drop_keys))
    return coefs, residual


def support_count(obs: list[dict], key: str) -> int:
    return sum(1 for o in obs if o[key])


def get_current(cfg: RenderConfig, path: list[str]) -> float:
    rt = cfg.frame_allocation.render_time
    if path == ["baseline_sec_cycles"]:
        return rt.baseline_sec_cycles
    if path == ["baseline_sec_eevee"]:
        return rt.baseline_sec_eevee
    if path[0] == "factors_cycles":
        return getattr(rt.factors_cycles, path[1])
    if path[0] == "factors_eevee":
        return getattr(rt.factors_eevee, path[1])
    raise KeyError(path)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    log.info("=" * 70)
    log.info("Time-formula calibration -- read-only analysis")
    log.info("=" * 70)

    log.info("\nLoading Firestore config/global ...")
    cfg = fetch_firestore_config()
    rt = cfg.frame_allocation.render_time
    log.info("  Current baseline_sec_cycles = %.2f", rt.baseline_sec_cycles)
    log.info("  Current baseline_sec_eevee  = %.2f", rt.baseline_sec_eevee)

    log.info("\nFetching render_telemetry ...")
    # render_telemetry rows are written by the success handler, so for every
    # row chunk_size == frames actually rendered.  A separate write-path bug
    # leaves rendered_frames stamped at 0 on some rows; fall back to chunk_size.
    rows = query_all(
        """
        SELECT id, fleet, gpu_type, chunk_size, rendered_frames,
               seconds_total, price_per_hour, cost_actual_usd,
               heaviness_json, file_size_bytes
          FROM render_telemetry
         WHERE seconds_total > 0
           AND chunk_size > 0
        """
    )
    log.info("  %d telemetry rows", len(rows))

    obs, skipped = build_observations(rows, cfg)
    log.info(
        "  Usable observations: %d  (skipped: %d unknown machine, %d zero time, %d missing heaviness)",
        len(obs), skipped["unknown_machine"], skipped["zero_time"], skipped["missing_heaviness"],
    )

    if len(obs) < 5:
        log.warning("\nToo few observations to fit anything meaningful. Exiting.")
        return 0

    log.info("\nFitting log-space linear regression ...")
    coefs, residual = fit(obs)

    rmse_log = float(np.sqrt(np.mean(residual ** 2)))
    mult_err = (math.exp(rmse_log) - 1.0) * 100.0
    log.info(
        "  RMSE (log space) = %.3f   (≈ %.0f%% per-row multiplicative error)",
        rmse_log, mult_err,
    )

    # ------------------------------------------------------------------
    log.info("\n=== Fitted coefficients ===")
    log.info("  %-40s %12s %12s %8s %s", "field", "current", "proposed", "N", "ratio")
    log.info("  " + "-" * 90)
    for key, json_name, patch_path in FEATURE_COLS:
        new_val = math.exp(coefs[key])
        n = support_count(obs, key)
        if patch_path is None:
            current = float("nan")
            ratio_str = "—"
            current_str = "(see note)"
        else:
            try:
                current = get_current(cfg, patch_path)
                ratio_str = f"{new_val/current:.2f}x" if current > 0 else "—"
                current_str = f"{current:.3f}"
            except KeyError:
                current_str = "?"
                ratio_str = "—"
        marker = "" if n >= CONFIDENCE_FLOOR else "  *low support, keep current"
        log.info("  %-40s %12s %12.3f %8d %s%s",
                 json_name, current_str, new_val, n, ratio_str, marker)

    # ------------------------------------------------------------------
    log.info("\n=== Per-machine residual ===")
    log.info("(positive residual = machine is slower than predicted → reduce render_speed)")
    log.info("  %-30s %5s %10s %10s %12s", "machine", "N", "mean_res", "cur_speed", "proposed")
    log.info("  " + "-" * 75)

    by_machine: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for o, r in zip(obs, residual):
        by_machine[o["machine"]].append((float(r), o["current_speed"]))

    machine_patch: list[dict] = []
    for machine, vals in sorted(by_machine.items(), key=lambda x: -len(x[1])):
        n = len(vals)
        mean_res = float(np.mean([v[0] for v in vals]))
        cur_speed = vals[0][1]
        new_speed = cur_speed * math.exp(-mean_res)
        marker = "" if n >= CONFIDENCE_FLOOR else "  *low support"
        log.info("  %-30s %5d %10.3f %10.2f %12.2f%s",
                 f"{machine[0]}|{machine[1]}", n, mean_res, cur_speed, new_speed, marker)
        if n >= CONFIDENCE_FLOOR and abs(mean_res) >= 0.1:
            machine_patch.append({
                "fleet": machine[0],
                "gpu_or_label": machine[1],
                "current_render_speed": round(cur_speed, 3),
                "proposed_render_speed": round(new_speed, 3),
                "n_chunks": n,
                "mean_log_residual": round(mean_res, 3),
            })

    # ------------------------------------------------------------------
    log.info("\n=== Proposed JSON patch ===")
    log.info("(low-support coefficients omitted -- keep current value for those)")

    render_time_patch: dict[str, Any] = {}
    factors_cycles: dict[str, float] = {}
    notes: list[str] = []

    for key, json_name, patch_path in FEATURE_COLS:
        n = support_count(obs, key)
        if n < CONFIDENCE_FLOOR:
            continue
        new_val = math.exp(coefs[key])
        if patch_path is None:
            notes.append(
                f"geometry_nodes regression multiplier = {new_val:.3f} "
                f"({n} rows). Current formula uses GEOMETRY_NODES_BASE=1.1 + "
                f"complexity ramp; map manually."
            )
            continue
        if patch_path == ["baseline_sec_cycles"]:
            render_time_patch["baseline_sec_cycles"] = round(new_val, 2)
        elif patch_path == ["baseline_sec_eevee"]:
            render_time_patch["baseline_sec_eevee"] = round(new_val, 2)
        elif patch_path[0] == "factors_cycles":
            factors_cycles[patch_path[1]] = round(new_val, 3)

    if factors_cycles:
        render_time_patch["factors_cycles"] = factors_cycles

    patch_output = {
        "frame_allocation": {
            "render_time": render_time_patch,
        },
    }

    print()
    print(json.dumps({
        "diagnostics": {
            "observations_used": len(obs),
            "rmse_log": round(rmse_log, 3),
            "approx_multiplicative_error_pct": round(mult_err, 1),
            "confidence_floor_rows": CONFIDENCE_FLOOR,
            "skipped": skipped,
        },
        "render_time_patch": patch_output,
        "per_machine_render_speed_patch": machine_patch,
        "manual_mapping_notes": notes,
    }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
