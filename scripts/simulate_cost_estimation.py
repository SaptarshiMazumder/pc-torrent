"""Offline cost-estimate simulator.

Loads the Firestore snapshot from serverV2/config/firestore_snapshot.txt,
wires it into the production allocation_time_analyzer module via the
same configure() entry point bootstrap uses, then replays every
historical telemetry row through the formula.  Prints predicted vs
actual time/cost per machine and overall.

Read-only: no Firestore writes, no Neon writes.  Edit the snapshot file,
re-run to see the new gap.

Usage:
    python -m scripts.simulate_cost_estimation
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict

from serverV2.allocation.render_config import RenderConfig
from serverV2.config import RenderTimeConfig
from serverV2.core.value_objects import parse_analysis_heaviness
from serverV2.infrastructure.db import query_all


SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "serverV2", "config", "firestore_snapshot.txt",
)

log = logging.getLogger("simulate_cost_estimation")


def load_snapshot() -> RenderConfig:
    if not os.path.isfile(SNAPSHOT_PATH):
        raise SystemExit(f"Snapshot file not found: {SNAPSHOT_PATH}")
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        raw = f.read()
    return RenderConfig.from_dict(json.loads(raw))


def wire_analyzer(cfg: RenderConfig) -> None:
    """Push the snapshot's render_time + weights into the production
    analyzer module, same as bootstrap.py does at boot."""
    from serverV2.allocation.allocation_strategies.analyzers import (
        allocation_time_analyzer,
    )
    rt = cfg.frame_allocation.render_time
    # RenderTimeSection (Firestore mirror) -> RenderTimeConfig (analyzer's
    # expected type).  Only the `startup_sec` -> `startup` field name
    # differs; contents are identical.
    analyzer_cfg = RenderTimeConfig(
        baseline_sec_cycles=rt.baseline_sec_cycles,
        baseline_sec_eevee=rt.baseline_sec_eevee,
        factors_cycles=rt.factors_cycles,
        factors_eevee=rt.factors_eevee,
        scene_scaling_cycles=rt.scene_scaling_cycles,
        scene_scaling_eevee=rt.scene_scaling_eevee,
        startup=rt.startup_sec,
    )
    allocation_time_analyzer.configure(analyzer_cfg)
    allocation_time_analyzer.configure_combination(
        secondary_feature_credit=cfg.frame_allocation.weights.secondary_feature_credit,
        heavy_multiplier_cap=cfg.frame_allocation.weights.heavy_multiplier_cap,
    )


def render_speed_for(cfg: RenderConfig, fleet: str, gpu_type: str) -> float | None:
    key = (gpu_type or "").strip().lower()
    if fleet == "vast_serverless":
        for v in cfg.vast_instances:
            for k in (v.label, v.gpu_name):
                if (k or "").strip().lower() == key:
                    return v.render_speed
    elif fleet == "modal_serverless":
        for m in cfg.modal_instances:
            for k in (m.label, m.gpu_type):
                if (k or "").strip().lower() == key:
                    return m.render_speed
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.info("=" * 78)
    log.info("Cost estimation simulator -- replays telemetry through current snapshot")
    log.info("=" * 78)

    log.info("\nLoading snapshot: %s", SNAPSHOT_PATH)
    cfg = load_snapshot()
    wire_analyzer(cfg)
    rt = cfg.frame_allocation.render_time
    fr = cfg.frame_allocation.failure_rate
    weights = cfg.frame_allocation.weights
    log.info("  baseline_sec_cycles      = %.2f", rt.baseline_sec_cycles)
    log.info("  baseline_sec_eevee       = %.2f", rt.baseline_sec_eevee)
    log.info("  secondary_feature_credit = %.2f", weights.secondary_feature_credit)
    log.info("  heavy_multiplier_cap     = %.2f", weights.heavy_multiplier_cap)
    log.info("  failure_rate.vast        = %.2f", fr.vast)
    log.info("  failure_rate.modal       = %.2f", fr.modal)
    log.info("  failure_rate.community   = %.2f", fr.community)

    log.info("\nFetching telemetry ...")
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

    from serverV2.allocation.allocation_strategies.analyzers import (
        allocation_time_analyzer,
    )

    failure_rate_by_fleet = {
        "vast_serverless": fr.vast,
        "modal_serverless": fr.modal,
        "community": fr.community,
    }
    buf = cfg.frame_allocation.startup_buffer_sec
    fleet_buffer_by_fleet = {
        "vast_serverless": buf.vast,
        "modal_serverless": buf.modal,
        "community": buf.community,
    }

    sims = []
    skipped_unknown = 0
    for r in rows:
        chunk_size = int(r["chunk_size"])
        seconds_total = float(r["seconds_total"])
        actual_cost = float(r["cost_actual_usd"])

        raw_h = r["heaviness_json"] or {}
        if not raw_h:
            skipped_unknown += 1
            continue
        heaviness = parse_analysis_heaviness(
            {"heaviness": raw_h},
            file_size_bytes=r.get("file_size_bytes"),
        )

        fleet = (r["fleet"] or "").strip()
        gpu_type = (r["gpu_type"] or "").strip()
        speed = render_speed_for(cfg, fleet, gpu_type)
        if speed is None or speed <= 0:
            skipped_unknown += 1
            continue

        spf = allocation_time_analyzer.estimate_seconds_per_frame(heaviness, speed)
        startup = allocation_time_analyzer.estimate_startup_seconds(heaviness)
        fleet_buffer = fleet_buffer_by_fleet.get(fleet, 0.0)
        # Matches chunk_seconds_for() in allocation_composite_scorer.py
        predicted_seconds_raw = startup + fleet_buffer + spf * chunk_size

        failure_widen = 1.0 + failure_rate_by_fleet.get(fleet, 0.0)
        predicted_seconds_widened = predicted_seconds_raw * failure_widen

        price = float(r["price_per_hour"])
        predicted_cost_raw = predicted_seconds_raw / 3600.0 * price
        predicted_cost_widened = predicted_seconds_widened / 3600.0 * price

        sims.append({
            "id": r["id"],
            "fleet": fleet,
            "gpu": gpu_type,
            "chunk_size": chunk_size,
            "actual_seconds": seconds_total,
            "predicted_seconds_raw": predicted_seconds_raw,
            "predicted_seconds_widened": predicted_seconds_widened,
            "actual_cost": actual_cost,
            "predicted_cost_raw": predicted_cost_raw,
            "predicted_cost_widened": predicted_cost_widened,
            "ratio_raw": predicted_seconds_raw / seconds_total,
            "ratio_widened": predicted_seconds_widened / seconds_total,
        })

    log.info("  Simulated %d rows (skipped %d unknown-machine)",
             len(sims), skipped_unknown)

    if not sims:
        log.warning("No simulatable rows. Exiting.")
        return 0

    # ------------------------------------------------------------------
    # Overall gap
    # ------------------------------------------------------------------
    total_actual = sum(s["actual_seconds"] for s in sims)
    total_raw = sum(s["predicted_seconds_raw"] for s in sims)
    total_widened = sum(s["predicted_seconds_widened"] for s in sims)
    actual_cost_sum = sum(s["actual_cost"] for s in sims)
    raw_cost_sum = sum(s["predicted_cost_raw"] for s in sims)
    widened_cost_sum = sum(s["predicted_cost_widened"] for s in sims)

    log.info("\n=== Overall ===")
    log.info("  rows:                          %d", len(sims))
    log.info("  actual seconds total:          %.0f", total_actual)
    log.info("  predicted seconds (raw):       %.0f  -> ratio %.2fx",
             total_raw, total_raw / total_actual)
    log.info("  predicted seconds (widened):   %.0f  -> ratio %.2fx",
             total_widened, total_widened / total_actual)
    log.info("  actual cost USD total:         %.3f", actual_cost_sum)
    log.info("  predicted cost USD (raw):      %.3f  -> ratio %.2fx",
             raw_cost_sum, raw_cost_sum / actual_cost_sum)
    log.info("  predicted cost USD (widened):  %.3f  -> ratio %.2fx",
             widened_cost_sum, widened_cost_sum / actual_cost_sum)

    # ------------------------------------------------------------------
    # Per-machine
    # ------------------------------------------------------------------
    by_machine: dict[tuple[str, str], list] = defaultdict(list)
    for s in sims:
        by_machine[(s["fleet"], s["gpu"])].append(s)

    log.info("\n=== Per machine (raw formula, no widening) ===")
    log.info("  %-36s %5s %10s %12s %12s %8s",
             "machine", "N", "act_sec_avg", "pred_sec_avg", "ratio_med", "ratio_p90")
    log.info("  " + "-" * 95)
    for (fleet, gpu), items in sorted(by_machine.items(),
                                       key=lambda x: -len(x[1])):
        n = len(items)
        act_avg = sum(s["actual_seconds"] for s in items) / n
        pred_avg = sum(s["predicted_seconds_raw"] for s in items) / n
        ratios = sorted(s["ratio_raw"] for s in items)
        ratio_med = ratios[n // 2]
        ratio_p90 = ratios[min(n - 1, int(n * 0.9))]
        log.info("  %-36s %5d %10.0f %12.0f %12.2fx %7.2fx",
                 f"{fleet}|{gpu}", n, act_avg, pred_avg, ratio_med, ratio_p90)

    # ------------------------------------------------------------------
    # Per-engine
    # ------------------------------------------------------------------
    log.info("\n=== Per engine (raw formula) ===")
    by_engine: dict[str, list] = defaultdict(list)
    for r, s in zip(rows, sims) if False else []:
        pass  # placeholder
    # Re-iterate from sims with heaviness re-parsed once for engine bucket
    engine_buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        s = next((x for x in sims if x["id"] == r["id"]), None)
        if s is None:
            continue
        h = r["heaviness_json"] or {}
        engine = str(h.get("render_engine") or "(unknown)")
        engine_buckets[engine].append(s)
    log.info("  %-20s %5s %12s %12s", "engine", "N", "ratio_med", "ratio_p90")
    log.info("  " + "-" * 55)
    for engine, items in sorted(engine_buckets.items(), key=lambda x: -len(x[1])):
        n = len(items)
        ratios = sorted(s["ratio_raw"] for s in items)
        med = ratios[n // 2]
        p90 = ratios[min(n - 1, int(n * 0.9))]
        log.info("  %-20s %5d %12.2fx %11.2fx", engine, n, med, p90)

    # ------------------------------------------------------------------
    # Worst offenders
    # ------------------------------------------------------------------
    log.info("\n=== Worst over-estimates (top 5 by ratio_raw) ===")
    worst = sorted(sims, key=lambda s: -s["ratio_raw"])[:5]
    log.info("  %-30s %5s %10s %10s %10s",
             "machine", "chunk", "act_sec", "pred_sec", "ratio")
    for s in worst:
        log.info("  %-30s %5d %10.0f %10.0f %9.1fx",
                 f"{s['fleet']}|{s['gpu']}", s["chunk_size"],
                 s["actual_seconds"], s["predicted_seconds_raw"],
                 s["ratio_raw"])

    log.info("\n=== Worst under-estimates (bottom 5 by ratio_raw) ===")
    best = sorted(sims, key=lambda s: s["ratio_raw"])[:5]
    log.info("  %-30s %5s %10s %10s %10s",
             "machine", "chunk", "act_sec", "pred_sec", "ratio")
    for s in best:
        log.info("  %-30s %5d %10.0f %10.0f %9.2fx",
                 f"{s['fleet']}|{s['gpu']}", s["chunk_size"],
                 s["actual_seconds"], s["predicted_seconds_raw"],
                 s["ratio_raw"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
