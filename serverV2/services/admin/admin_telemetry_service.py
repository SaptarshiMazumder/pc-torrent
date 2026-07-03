"""AdminTelemetryService — read-only aggregation layer for the admin dashboard.

Every method backs one ``/admin/*`` endpoint.  Read strategy mirrors the
rest of the codebase:

  * Live operational state comes from Redis (active-group mirror, machine
    liveness/status, job heartbeats, fleet snapshot cache, daemon locks).
  * Historical aggregates (costs, per-user rollups, failures) are SQL over
    Neon, cached in Redis with a short TTL so a dashboard poll never
    hammers Postgres.
  * Everything fails open: a Redis blip degrades a panel, never 500s it.

Strictly read-only — no method here mutates any store (the cost/user
caches are the one write, and they're throwaway).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from serverV2.infrastructure.db import query_all, query_one
from serverV2.infrastructure.redis_client import RedisClient, namespaced
from serverV2.repositories.heartbeat_repository import HeartbeatRepository
from serverV2.repositories.job_repository import JobRepository
from serverV2.repositories.telemetry_repository import TelemetryRepository
from serverV2.services.admin.daemon_status import DaemonStatusRepository, KNOWN_DAEMONS
from serverV2.services.admin.download_stats import DownloadStatsRepository
from serverV2.services.machines.machine_redis_mirror import MachineRedisMirror
from serverV2.fleets.fleet_availability.fleet_availability_snapshot_cache import (
    FleetAvailabilitySnapshotCache,
)
from serverV2.fleets.status_aggregator import InstanceStatusAggregator
from serverV2.users import UserFacade

log = logging.getLogger(__name__)

_ACTIVE_GROUP_STATUSES = ("uploading", "pending", "running")
_ACTIVE_JOB_STATUSES = ("pending", "running")

_CACHE_TTL_SEC = 60


def _jsonable(value: Any) -> Any:
    """Recursively coerce SQL row values (Decimal / datetime) to JSON types."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class AdminTelemetryService:

    def __init__(
        self,
        *,
        redis_client: RedisClient,
        machine_redis_mirror: MachineRedisMirror,
        fleet_snapshot_cache: FleetAvailabilitySnapshotCache,
        job_repo: JobRepository,
        heartbeat_repo: HeartbeatRepository,
        user_facade: UserFacade,
        download_stats: DownloadStatsRepository,
        telemetry_repo: TelemetryRepository,
        status_aggregator: InstanceStatusAggregator,
        daemon_status: DaemonStatusRepository,
    ) -> None:
        self._redis_client = redis_client
        self._machine_mirror = machine_redis_mirror
        self._fleet_snapshot_cache = fleet_snapshot_cache
        self._job_repo = job_repo
        self._heartbeat_repo = heartbeat_repo
        self._users = user_facade
        self._download_stats = download_stats
        self._telemetry_repo = telemetry_repo
        self._status_aggregator = status_aggregator
        self._daemon_status = daemon_status

    # ------------------------------------------------------------------
    # small Redis helpers (fail-open)
    # ------------------------------------------------------------------

    def _redis_ok(self) -> bool:
        client = self._redis_client.client()
        if client is None:
            return False
        try:
            return bool(client.ping())
        except Exception:
            return False

    def _redis_get(self, key: str) -> str | None:
        client = self._redis_client.client()
        if client is None:
            return None
        try:
            return client.get(key)
        except Exception:
            return None

    def _cache_read(self, key: str) -> Any | None:
        raw = self._redis_get(namespaced(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def _cache_write(self, key: str, payload: Any) -> None:
        client = self._redis_client.client()
        if client is None:
            return
        try:
            client.set(namespaced(key), json.dumps(payload), ex=_CACHE_TTL_SEC)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # overview
    # ------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        db_ok = True
        try:
            query_one("SELECT 1")
        except Exception:
            db_ok = False

        counts: dict[str, Any] = {}
        if db_ok:
            try:
                counts = {
                    "active_groups": (query_one(
                        "SELECT COUNT(*) AS n FROM render_groups WHERE status IN %s",
                        (_ACTIVE_GROUP_STATUSES,),
                    ) or {}).get("n", 0),
                    "active_jobs_by_fleet": self._job_repo.count_active_by_fleet(),
                    "dispatch_queue_depth": (query_one(
                        "SELECT COUNT(*) AS n FROM dispatch_queue",
                    ) or {}).get("n", 0),
                    "pending_allocation_depth": (query_one(
                        "SELECT COUNT(*) AS n FROM pending_allocation_queue",
                    ) or {}).get("n", 0),
                    "failures_24h": (query_one(
                        "SELECT COUNT(*) AS n FROM render_telemetry "
                        "WHERE failure_reason IS NOT NULL AND completed_at >= %s",
                        (self._iso_hours_ago(24),),
                    ) or {}).get("n", 0),
                }
            except Exception as exc:
                log.warning("Admin overview counts failed: %s", exc)

        alive = self._machine_mirror.alive_map()

        # Daemon health from per-tick heartbeats (a single HGETALL) instead of
        # four lock GETs -- richer AND cheaper.  A daemon only heartbeats while
        # it owns its lock, so a present, recent heartbeat IS the live owner
        # plus what it did last cycle.  No heartbeat -> not running anywhere.
        statuses = self._daemon_status.read_all()
        now_ts = datetime.now(timezone.utc).timestamp()
        daemons = []
        for name in KNOWN_DAEMONS:
            s = statuses.get(name)
            if s is None:
                daemons.append({"name": name, "reporting": False})
                continue
            last = s.get("last_tick_ts")
            daemons.append({
                "name": name,
                "reporting": True,
                "owner": s.get("owner"),
                "tick_count": s.get("tick_count"),
                "last_tick_ts": last,
                "age_sec": (now_ts - last) if last else None,
                "detail": s.get("detail") or {},
                "error": s.get("error"),
            })

        fleet: dict[str, Any] | None = None
        snapshot = self._fleet_snapshot_cache.peek()
        if snapshot is not None:
            fleet = {
                "vast_available": len(snapshot.vast_available),
                "modal_available": len(snapshot.modal_available),
                "community_available": len(snapshot.community_available),
                "serverless_in_flight": dict(snapshot.serverless_in_flight),
            }

        return _jsonable({
            "redis_ok": self._redis_ok(),
            "db_ok": db_ok,
            "counts": counts,
            "daemons": daemons,
            "fleet_availability": fleet,
            "machines_alive": len(alive) if alive is not None else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    @staticmethod
    def _iso_hours_ago(hours: int) -> str:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def _credits_per_usd(self) -> float:
        """Live conversion rate for projecting USD aggregates into tokens
        (credits).  Reads through the user facade; cheap, config-backed."""
        try:
            return float(self._users.credits_per_usd)
        except Exception:
            return 0.0

    def _user_index(self) -> dict[str, dict[str, str]]:
        """``{uid: {"email": ..., "display_name": ...}}`` for annotating live
        rows with a human identity.  Cached 60s in Redis so the 5s live polls
        don't hit Firestore every tick."""
        cached = self._cache_read("admin:user_index")
        if cached is not None:
            return cached
        index: dict[str, dict[str, str]] = {}
        try:
            for p in self._users.list_profiles():
                uid = p.get("uid")
                if uid:
                    index[uid] = {
                        "email": p.get("email", "") or "",
                        "display_name": p.get("display_name", "") or "",
                    }
        except Exception as exc:
            log.warning("Admin user index build failed: %s", exc)
        self._cache_write("admin:user_index", index)
        return index

    @staticmethod
    def _annotate_user(row: dict[str, Any], index: dict[str, dict[str, str]]) -> dict[str, Any]:
        info = index.get(row.get("user_id") or "", {})
        return {
            **row,
            "user_email": info.get("email", ""),
            "user_display_name": info.get("display_name", ""),
        }

    # ------------------------------------------------------------------
    # live renders / jobs / machines
    # ------------------------------------------------------------------

    def active_render_groups(self) -> list[dict[str, Any]]:
        """Every user's active groups, read from Postgres (the source of
        truth).  Each row is annotated with the owner's email / display name.

        Read-only: the per-user Redis mirror is env-scoped by uid and only
        answers "this user's groups", so the all-users admin view queries
        Postgres directly rather than imposing a global mirror write."""
        index = self._user_index()
        try:
            rows = query_all(
                """
                SELECT id AS group_id, user_id, status, tier, input_filename,
                       total_frames, overall_rendered_frames, submitted_at,
                       tasks_count
                  FROM render_groups
                 WHERE status IN %s
                 ORDER BY submitted_at DESC
                 LIMIT 100
                """,
                (_ACTIVE_GROUP_STATUSES,),
            )
        except Exception as exc:
            log.warning("Admin active groups fallback failed: %s", exc)
            return []
        return [self._annotate_user(_jsonable(row), index) for row in rows]

    def active_jobs(self) -> list[dict[str, Any]]:
        """Pending/running jobs with owner + live heartbeat state."""
        try:
            rows = query_all(
                """
                SELECT j.id, j.group_id, j.chunk_index, j.attempt, j.status,
                       j.machine_type, j.gpu_type, j.machine_id,
                       j.submitted_at, j.started_at, j.heartbeat_phase,
                       j.last_heartbeat_at, j.rendered_frames, j.total_frames,
                       j.estimated_cost_usd, j.price_per_hour_at_dispatch,
                       g.user_id, g.input_filename
                  FROM jobs j
                  LEFT JOIN render_groups g ON g.id = j.group_id
                 WHERE j.status IN %s
                 ORDER BY j.submitted_at DESC
                 LIMIT 200
                """,
                (_ACTIVE_JOB_STATUSES,),
            )
        except Exception as exc:
            log.warning("Admin active jobs query failed: %s", exc)
            return []

        index = self._user_index()
        out = []
        for row in rows:
            job_id = row["id"]
            samples = self._heartbeat_repo.get_recent(job_id, n=1)
            latest = samples[0] if samples else None
            heartbeat_age_sec = None
            if latest and latest.get("ts"):
                heartbeat_age_sec = max(
                    0.0,
                    datetime.now(timezone.utc).timestamp() - float(latest["ts"]),
                )
            out.append(self._annotate_user(_jsonable({
                **row,
                "heartbeat_alive": self._heartbeat_repo.is_alive(job_id),
                "heartbeat_age_sec": heartbeat_age_sec,
                "heartbeat_sample": latest,
            }), index))
        return out

    def machines(self) -> list[dict[str, Any]]:
        """Community machine rows merged with Redis liveness + status."""
        try:
            rows = query_all(
                """
                SELECT id, gpu_model, gpu_vram_gb, cpu_cores, ram_gb, status,
                       machine_type, user_id, render_speed, registered_at,
                       last_seen_at, commitment_end_at
                  FROM machines
                 ORDER BY last_seen_at DESC NULLS LAST
                 LIMIT 200
                """,
            )
        except Exception as exc:
            log.warning("Admin machines query failed: %s", exc)
            rows = []

        alive = self._machine_mirror.alive_map() or {}
        statuses = self._machine_mirror.status_map() or {}
        now_ts = datetime.now(timezone.utc).timestamp()
        out = []
        for row in rows:
            mid = row["id"]
            last_hb = alive.get(mid)
            out.append(_jsonable({
                **row,
                "redis_status": statuses.get(mid),
                "alive": last_hb is not None,
                "heartbeat_age_sec": (now_ts - last_hb) if last_hb else None,
            }))
        return out

    # ------------------------------------------------------------------
    # live serverless instances (in-process registry + jobs join)
    # ------------------------------------------------------------------

    def serverless_instances(self) -> list[dict[str, Any]]:
        """Currently-running Vast/Modal containers from the in-process
        InstanceStatusAggregator, enriched with each job's owner, input file,
        dispatch price, and a computed live cost-so-far (elapsed x rate).

        The registry only holds in-flight serverless jobs and is wiped when a
        job goes terminal, so this is strictly a "what's running now" view."""
        if self._status_aggregator is None:
            return []
        snaps: list[dict[str, Any]] = []
        for fleet, lst in (self._status_aggregator.get_all() or {}).items():
            for s in lst or []:
                snaps.append({**s, "fleet_type": s.get("fleet_type") or fleet})
        if not snaps:
            return []

        job_ids = [s["job_id"] for s in snaps if s.get("job_id")]
        rows: dict[str, dict] = {}
        if job_ids:
            try:
                fetched = query_all(
                    """
                    SELECT j.id, j.started_at, j.submitted_at, j.gpu_type,
                           j.price_per_hour_at_dispatch, g.user_id, g.input_filename
                      FROM jobs j
                      LEFT JOIN render_groups g ON g.id = j.group_id
                     WHERE j.id = ANY(%s)
                    """,
                    (job_ids,),
                )
                rows = {r["id"]: r for r in fetched}
            except Exception as exc:
                log.warning("Admin serverless_instances jobs join failed: %s", exc)

        rate = self._credits_per_usd()
        index = self._user_index()
        out = []
        for s in snaps:
            job = rows.get(s.get("job_id"), {})
            elapsed = s.get("elapsed_sec")
            price = job.get("price_per_hour_at_dispatch")
            cost_usd = None
            if elapsed is not None and price is not None:
                cost_usd = float(elapsed) / 3600.0 * float(price)
            rec = {
                "job_id": s.get("job_id"),
                "fleet_type": s.get("fleet_type"),
                "provider_status": s.get("provider_status"),
                "gpu_label": s.get("gpu_label") or job.get("gpu_type"),
                "rendered_frames": s.get("rendered_frames"),
                "total_frames": s.get("total_frames"),
                "elapsed_sec": elapsed,
                "error": s.get("error"),
                "has_logs": bool(s.get("logs")),
                "user_id": job.get("user_id"),
                "input_filename": job.get("input_filename"),
                "started_at": job.get("started_at"),
                "price_per_hour": float(price) if price is not None else None,
                "live_cost_usd": cost_usd,
                "live_cost_credits": (cost_usd * rate) if cost_usd is not None else None,
            }
            out.append(self._annotate_user(rec, index))
        out.sort(key=lambda r: r.get("elapsed_sec") or 0, reverse=True)
        return [_jsonable(r) for r in out]

    # ------------------------------------------------------------------
    # per-render telemetry detail (row-level render_telemetry read)
    # ------------------------------------------------------------------

    def render_telemetry(self, limit: int = 100, group_id: str | None = None) -> list[dict[str, Any]]:
        """Per-completed-chunk detail: memory, GPU, device, timing, cost,
        datetimes.  Scoped to one group when ``group_id`` is given, else the
        most recent chunks across all renders."""
        try:
            rows = (
                self._telemetry_repo.by_group(group_id)
                if group_id
                else self._telemetry_repo.recent(limit)
            )
        except Exception as exc:
            log.warning("Admin render_telemetry read failed: %s", exc)
            return []
        rate = self._credits_per_usd()
        index = self._user_index()
        out = []
        for r in rows:
            enriched = {
                **r,
                "cost_actual_credits": float(r.get("cost_actual_usd") or 0) * rate,
            }
            # by_group rows have no user_id column; annotate is a no-op there.
            out.append(self._annotate_user(enriched, index))
        return [_jsonable(r) for r in out]

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------

    def users(self) -> list[dict[str, Any]]:
        cached = self._cache_read("admin:users")
        if cached is not None:
            return cached

        rate = self._credits_per_usd()
        profiles = []
        try:
            profiles = self._users.list_profiles()
        except Exception as exc:
            log.warning("Admin users profile list failed: %s", exc)

        rollups: dict[str, dict[str, Any]] = {}
        try:
            rows = query_all(
                """
                SELECT user_id,
                       COUNT(*) AS groups_count,
                       COUNT(*) FILTER (WHERE status IN %s) AS active_groups,
                       COALESCE(SUM(total_actual_cost_usd), 0) AS actual_cost_usd,
                       MAX(submitted_at) AS last_submitted_at
                  FROM render_groups
                 WHERE user_id IS NOT NULL
                 GROUP BY user_id
                """,
                (_ACTIVE_GROUP_STATUSES,),
            )
            rollups = {row["user_id"]: row for row in rows}
        except Exception as exc:
            log.warning("Admin users rollup query failed: %s", exc)

        out = []
        seen_uids = set()
        for profile in profiles:
            uid = profile.get("uid", "")
            seen_uids.add(uid)
            rollup = rollups.get(uid, {})
            out.append(_jsonable({
                "uid": uid,
                "email": profile.get("email", ""),
                "display_name": profile.get("display_name", ""),
                "tier": profile.get("tier", "free"),
                "role": profile.get("role", "user"),
                "credits": profile.get("credits", 0.0),
                "created_at": profile.get("created_at"),
                "groups_count": rollup.get("groups_count", 0),
                "active_groups": rollup.get("active_groups", 0),
                "actual_cost_usd": rollup.get("actual_cost_usd", 0),
                "actual_cost_credits": float(rollup.get("actual_cost_usd", 0) or 0) * rate,
                "last_submitted_at": rollup.get("last_submitted_at"),
            }))
        # Postgres knows users Firestore doesn't (deleted docs, legacy rows).
        for uid, rollup in rollups.items():
            if uid in seen_uids:
                continue
            out.append(_jsonable({
                "uid": uid,
                "email": "",
                "display_name": "(no profile)",
                "tier": None,
                "role": None,
                "credits": None,
                "created_at": None,
                "groups_count": rollup.get("groups_count", 0),
                "active_groups": rollup.get("active_groups", 0),
                "actual_cost_usd": rollup.get("actual_cost_usd", 0),
                "actual_cost_credits": float(rollup.get("actual_cost_usd", 0) or 0) * rate,
                "last_submitted_at": rollup.get("last_submitted_at"),
            }))
        out.sort(key=lambda u: u.get("last_submitted_at") or "", reverse=True)
        self._cache_write("admin:users", out)
        return out

    def user_detail(self, uid: str) -> dict[str, Any]:
        profile: dict[str, Any] = {}
        spend: list[dict[str, Any]] = []
        try:
            profile = self._users.get_profile(uid) or {}
            spend = self._users.list_spend(uid)
        except Exception as exc:
            log.warning("Admin user_detail(%s) Firestore read failed: %s", uid, exc)

        rate = self._credits_per_usd()
        groups: list[dict[str, Any]] = []
        try:
            groups = query_all(
                """
                SELECT id, status, tier, input_filename, total_frames,
                       overall_rendered_frames, submitted_at, completed_at,
                       total_actual_cost_usd, error
                  FROM render_groups
                 WHERE user_id = %s
                 ORDER BY submitted_at DESC
                 LIMIT 25
                """,
                (uid,),
            )
        except Exception as exc:
            log.warning("Admin user_detail(%s) groups query failed: %s", uid, exc)

        groups = [
            {**g, "total_actual_cost_credits": float(g.get("total_actual_cost_usd") or 0) * rate}
            for g in groups
        ]

        # Never leak balance-of-system fields the dashboard doesn't need.
        safe_profile = {
            k: profile.get(k)
            for k in ("email", "display_name", "tier", "role", "credits",
                      "created_at", "updated_at")
        }
        return _jsonable({
            "uid": uid,
            "profile": safe_profile,
            "recent_spend": spend,
            "recent_groups": groups,
        })

    # ------------------------------------------------------------------
    # costs
    # ------------------------------------------------------------------

    def costs_summary(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        cache_key = f"admin:costs:{days}"
        cached = self._cache_read(cache_key)
        if cached is not None:
            return cached

        window = "t.completed_at >= NOW() - make_interval(days => %s)"
        try:
            totals = query_one(
                f"""
                SELECT COUNT(*) AS chunks,
                       COALESCE(SUM(t.cost_actual_usd), 0) AS actual_usd,
                       COALESCE(SUM(t.cost_estimated_usd), 0) AS estimated_usd,
                       COALESCE(SUM(t.seconds_total), 0) AS seconds_total,
                       COALESCE(SUM(t.rendered_frames), 0) AS frames
                  FROM render_telemetry t
                 WHERE {window}
                """,
                (days,),
            ) or {}
            by_fleet = query_all(
                f"""
                SELECT t.fleet,
                       COUNT(*) AS chunks,
                       COALESCE(SUM(t.cost_actual_usd), 0) AS actual_usd,
                       COALESCE(SUM(t.seconds_total), 0) AS seconds_total
                  FROM render_telemetry t
                 WHERE {window}
                 GROUP BY t.fleet
                 ORDER BY actual_usd DESC
                """,
                (days,),
            )
            by_gpu = query_all(
                f"""
                SELECT COALESCE(t.gpu_model_normalized, t.gpu_type, 'unknown') AS gpu,
                       COUNT(*) AS chunks,
                       COALESCE(SUM(t.cost_actual_usd), 0) AS actual_usd,
                       COALESCE(SUM(t.seconds_total), 0) AS seconds_total
                  FROM render_telemetry t
                 WHERE {window}
                 GROUP BY 1
                 ORDER BY actual_usd DESC
                 LIMIT 15
                """,
                (days,),
            )
            by_day = query_all(
                f"""
                SELECT DATE(t.completed_at) AS day,
                       COUNT(*) AS chunks,
                       COALESCE(SUM(t.cost_actual_usd), 0) AS actual_usd
                  FROM render_telemetry t
                 WHERE {window}
                 GROUP BY 1
                 ORDER BY 1
                """,
                (days,),
            )
            top_users = query_all(
                f"""
                SELECT g.user_id,
                       COUNT(*) AS chunks,
                       COALESCE(SUM(t.cost_actual_usd), 0) AS actual_usd
                  FROM render_telemetry t
                  JOIN render_groups g ON g.id = t.group_id
                 WHERE {window} AND g.user_id IS NOT NULL
                 GROUP BY g.user_id
                 ORDER BY actual_usd DESC
                 LIMIT 10
                """,
                (days,),
            )
        except Exception as exc:
            log.warning("Admin costs query failed: %s", exc)
            return {"days": days, "error": "cost query failed"}

        # Project every USD aggregate into tokens (credits) so the dashboard
        # can show tokens alongside cost.  One rate applied uniformly, matching
        # how per-render credits are computed elsewhere.
        rate = self._credits_per_usd()

        def with_credits(row, usd_key="actual_usd", credit_key="actual_credits"):
            return {**row, credit_key: float(row.get(usd_key) or 0) * rate}

        totals = with_credits(totals)
        totals = with_credits(totals, "estimated_usd", "estimated_credits")

        index = self._user_index()
        top_users = [
            self._annotate_user(with_credits(row), index) for row in top_users
        ]

        payload = _jsonable({
            "days": days,
            "credits_per_usd": rate,
            "totals": totals,
            "by_fleet": [with_credits(row) for row in by_fleet],
            "by_gpu": [with_credits(row) for row in by_gpu],
            "by_day": [
                {**with_credits(row), "day": str(row.get("day"))} for row in by_day
            ],
            "top_users": top_users,
        })
        self._cache_write(cache_key, payload)
        return payload

    # ------------------------------------------------------------------
    # failures / downloads
    # ------------------------------------------------------------------

    def failures_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent chunk failures, read from the ``render_telemetry`` rows that
        already record a ``failure_reason`` (the source of truth).  Read-only —
        no separate failure_events audit table is written."""
        limit = max(1, min(int(limit), 200))
        try:
            rows = query_all(
                """
                SELECT t.id, t.completed_at, t.fleet, t.job_id, t.group_id,
                       t.machine_id, t.gpu_type, t.retry_count,
                       t.failure_reason, g.user_id, g.input_filename
                  FROM render_telemetry t
                  LEFT JOIN render_groups g ON g.id = t.group_id
                 WHERE t.failure_reason IS NOT NULL
                 ORDER BY t.completed_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
        except Exception as exc:
            log.warning("Admin failures query failed: %s", exc)
            return []
        index = self._user_index()
        return [self._annotate_user(_jsonable(row), index) for row in rows]

    def downloads(self) -> dict[str, dict]:
        return self._download_stats.totals()

    # ------------------------------------------------------------------
    # redis command activity (quota / cost observability)
    # ------------------------------------------------------------------

    def redis_activity(self, recent_limit: int = 200) -> dict[str, Any]:
        """Live Redis command stream + quota-burn stats.  Pure in-memory read
        of this process's recorded command traffic (adds NO Redis ops), so
        watching it never inflates the metric it measures."""
        act = self._redis_client.activity()
        if act is None:
            return {"enabled": False}
        return {"enabled": True, **act.snapshot(recent_limit=recent_limit)}

    def redis_activity_reset(self) -> dict[str, Any]:
        act = self._redis_client.activity()
        if act is not None:
            act.reset()
        return {"reset": act is not None}
