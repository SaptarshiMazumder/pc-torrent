"""JobService — worker-callback handling, output management, community pull.

Composes: job_repo, heartbeat_repo, progress_repo, worker_start_repo, outputs_resolver.
Pure DB operations — zero orchestration calls.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any, Callable

from serverV2.core.value_objects import sanitize_filename
from serverV2.infrastructure import storage
from serverV2.repositories.output_frame_repository import OutputFrameRepository
from serverV2.services.jobs.outputs_resolver import OutputsResolver
from serverV2.services.machines.machine_state_writer import MachineStateWriter

log = logging.getLogger(__name__)


class JobServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class JobTerminalError(JobServiceError):
    """Raised by worker-callback methods when the job has already reached
    a terminal status (done / failed / cancelled).  Routed to HTTP 410
    Gone with a structured body so worker clients can detect terminal
    state and exit cleanly instead of retrying the call.
    """

    def __init__(self, job_id: str, current_status: str, reason: str) -> None:
        super().__init__(410, f"Job {job_id} is {current_status}: {reason}")
        self.job_id = job_id
        self.current_status = current_status
        self.reason = reason

    def to_body(self) -> dict[str, str]:
        return {
            "error": "job_terminal",
            "status": self.current_status,
            "reason": self.reason,
        }


class JobService:

    def __init__(
        self,
        *,
        job_repo,
        machine_repo,
        heartbeat_repo,
        machine_heartbeat_repo,
        machine_state_writer: MachineStateWriter,
        progress_repo,
        worker_start_repo,
        outputs_resolver: OutputsResolver,
        output_frame_repo: OutputFrameRepository,
        success_notifier: Callable[[str], None],
        community_idle_notifier: Callable[[str], None],
        terminal_cache=None,
    ) -> None:
        self._jobs = job_repo
        self._machines = machine_repo
        self._heartbeats = heartbeat_repo
        self._machine_hb = machine_heartbeat_repo
        self._state_writer = machine_state_writer
        self._progress = progress_repo
        self._worker_start = worker_start_repo
        self._outputs = outputs_resolver
        self._output_frames = output_frame_repo
        # Redis-backed terminal-status cache.  ``_assert_not_terminal``
        # reads from here so the heartbeat path doesn't do a Postgres
        # SELECT on every call.
        self._terminal_cache = terminal_cache
        # Called when ``register_outputs`` observes that the verified
        # upload count meets total_frames.  Wired to CallbackRouter in
        # bootstrap so completion routes through the standard success
        # path (handle_chunk_succeeded → mark_done, telemetry, drain,
        # group reconcile).  No worker self-report of "done"; no monitor
        # poll race — the data is the success signal.
        self._success_notifier = success_notifier
        # Called from ``next_for_machine``'s self-heal when a polling
        # agent is found with status='processing'.  Wired to
        # ``orchestrator.handle_community_machine_idle`` in bootstrap
        # so any zombie running-job assigned to this machine gets
        # marked failed before we flip the row back to 'available'.
        self._community_idle_notifier = community_idle_notifier

    # ---- duplicate-start guard for serverless containers ----

    def try_claim_worker_start(self, job_id: str) -> bool:
        """Called once per container start.  True if this caller is the
        first to claim, False if another container already claimed this
        ``job_id`` — the worker must abort to defeat Modal's re-queue.
        """
        return self._worker_start.try_claim(job_id)

    # ---- status callbacks (from workers) ----

    _TERMINAL_STATUSES = frozenset({"cancelled", "done", "failed"})

    def _assert_not_terminal(self, job_id: str) -> None:
        """Guard for the heartbeat callback.  Reads from the Redis
        terminal-status cache (``JobTerminalCache``) -- no Postgres
        round-trip per heartbeat.  Cache is populated by every
        ``JobRepository`` method that transitions a row to a terminal
        status, so a worker hitting this guard sees the flag within
        milliseconds of the orchestrator's terminal write.

        Cache miss (Redis down, or status was set outside JobRepository
        somehow) is treated as "not terminal" -- fail-open.  Workers
        keep heartbeating; provider-side container kills + the cancel-
        status poll path catch zombies that this guard misses.
        """
        if self._terminal_cache is None:
            return
        if self._terminal_cache.is_terminal(job_id):
            raise JobTerminalError(
                job_id=job_id,
                current_status="terminal",
                reason="orchestrator already considers this job terminal",
            )

    def update_status(self, job_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")

        current = str(job.get("status") or "")
        if current in self._TERMINAL_STATUSES:
            log.info("Rejecting status update %s→%s for job %s (terminal)", current, status, job_id)
            return {"job_id": job_id, "status": current, "success": False, "reason": "job already terminal"}

        self._jobs.update_status(job_id, status, error=error)
        # When the worker self-reports ``done`` (community path -- closes
        # the reclaim race where a sidecar restart between "last upload"
        # and "success_notifier BackgroundTask firing" leaves the row
        # status='running' for handle_community_machine_idle to mark
        # failed), tell the caller to schedule the success chain as a
        # BackgroundTask.  We deliberately do NOT call success_notifier
        # synchronously here -- the heavy work (telemetry, group
        # reconcile, drain) would inflate the worker's HTTP response
        # time and risk client-side timeout.  The synchronous DB write
        # above is enough to close the race; the rest can run async,
        # exactly mirroring the existing register_outputs flow.
        needs_completion = status == "done"
        return {"job_id": job_id, "status": status, "needs_completion": needs_completion}

    def update_progress(self, job_id: str, rendered_frames: int, total_frames: int) -> dict[str, Any]:
        self._progress.record(job_id, rendered_frames, total_frames)
        return {"job_id": job_id, "rendered_frames": rendered_frames, "total_frames": total_frames}

    def heartbeat(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        cpu_percent: float | None = None,
        rss_bytes: int | None = None,
        bytes_progressed: int | None = None,
        total_bytes: int | None = None,
    ) -> dict[str, Any]:
        self._assert_not_terminal(job_id)
        self._heartbeats.record(
            job_id,
            phase=phase,
            cpu_percent=cpu_percent,
            rss_bytes=rss_bytes,
            bytes_progressed=bytes_progressed,
            total_bytes=total_bytes,
        )
        return {"job_id": job_id, "acknowledged": True}

    def request_upload_urls(
        self, job_id: str, filenames: list[str],
    ) -> dict[str, str]:
        """Mint presigned R2 PUT URLs for the worker's output files.

        Used to live inline in the router doing raw SQL + storage calls;
        moved here as a layering cleanup (routers do HTTP only).  No
        terminal-status guard -- the heartbeat / progress 410 mechanism
        is what tells a worker to exit; this endpoint stays in its
        original always-mint shape.
        """
        from serverV2.core.value_objects import sanitize_filename
        from serverV2.infrastructure import storage

        if not filenames:
            raise JobServiceError(400, "No filenames provided")
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        urls = {
            fname: storage.generate_presigned_upload_url(
                f"jobs/{group_id}/output/{sanitize_filename(fname)}",
                expires_in=3600,
            )
            for fname in filenames
        }
        return urls

    # ---- cancel-status poll (workers check this; widened to include `done`
    # ---- so a community sibling marking the chunk done aborts in-flight
    # ---- duplicates, and serverless-respawn workers see "no work" at
    # ---- startup via the same poll endpoint) -------------------------

    _CANCEL_STATUSES = frozenset({"cancelled", "failed", "done"})

    def get_cancel_status(self, job_id: str) -> dict[str, Any]:
        """Tells a long-running worker whether to abort.  ``cancelled=true``
        when the orchestrator has marked the job ``cancelled`` (user action)
        OR ``failed`` (retry exhausted, staleness, etc.).  Either way the
        worker should stop wasting compute on a job nobody wants anymore.
        """
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        cancelled = (job.get("status") or "") in self._CANCEL_STATUSES
        return {"job_id": job_id, "cancelled": cancelled}

    # ---- output management ----

    def register_outputs(self, job_id: str, files: list[str]) -> dict[str, Any]:
        """Pure DB work: insert into ``output_frames`` (PK on
        (group_id, filename) silently dedupes sibling-retry duplicates),
        return this job's full filename list and a flag telling the
        caller whether this registration completed the chunk.  The
        downstream side-effects (telemetry, group reconcile, drain
        queue, possibly new dispatch HTTP calls) are NOT fired here —
        the route schedules :meth:`notify_completion` as a background
        task so the worker's HTTP response doesn't block on them.
        Keeping the response under 100ms prevents the catch-up path
        from misclassifying a slow side-effect chain as a failed
        upload."""
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        self._output_frames.add_many(group_id, job_id, files)
        merged = self._output_frames.list_for_job(job_id)
        total = job.get("total_frames") or 0
        completion_reached = total > 0 and len(merged) >= total
        return {
            "job_id": job_id,
            "output_files": merged,
            "completion_reached": completion_reached,
        }

    def notify_completion(self, job_id: str) -> None:
        """Run the success-notifier chain (handle_chunk_succeeded ->
        telemetry, ledger release, group reconcile, drain).  Idempotent
        via ``handle_chunk_succeeded``'s ``status==done`` short-circuit,
        so duplicate fires (e.g. background task + a parallel monitor
        observing completion via JobCounts) collapse to one transition.
        Called as a FastAPI BackgroundTask from the register-outputs
        route after the worker's HTTP response has been sent."""
        self._success_notifier(job_id)

    def get_outputs(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        scope_id = job.get("group_id") or job_id
        entries = self._outputs.entries([job], scope_id=scope_id)
        return {"job_id": job_id, "files": entries, "count": len(entries)}

    def get_output_download_url(self, job_id: str, filename: str) -> str:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        key = f"jobs/{group_id}/output/{filename}"
        if not storage.file_exists(key):
            raise JobServiceError(404, "Output file not found")
        return storage.generate_presigned_url(key, download_name=filename)

    def get_output_preview(self, job_id: str, filename: str) -> tuple[bytes, str]:
        from serverV2.services.previews.renderer import (
            PREVIEW_MEDIA_TYPE,
            PreviewRenderError,
            generate_image_preview,
        )
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        key = f"jobs/{group_id}/output/{sanitize_filename(filename)}"
        if not storage.file_exists(key):
            raise JobServiceError(404, "Output file not found")
        try:
            raw = storage.download_file(key)
        except Exception as exc:
            raise JobServiceError(404, "Output file not found") from exc
        try:
            return generate_image_preview(raw), PREVIEW_MEDIA_TYPE
        except PreviewRenderError as exc:
            raise JobServiceError(415, str(exc)) from exc

    def download_all_as_zip(self, job_id: str) -> io.BytesIO:
        job = self._jobs.get_raw_by_id(job_id)
        if not job:
            raise JobServiceError(404, "Job not found")
        group_id = job.get("group_id") or job_id
        output_files = self._output_frames.list_for_job(job_id)
        if not output_files:
            raise JobServiceError(404, "No output files available")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in output_files:
                key = f"jobs/{group_id}/output/{fname}"
                try:
                    data = storage.download_file(key)
                    zf.writestr(fname, data)
                except Exception as exc:
                    log.warning("Skipping %s in ZIP: %s", fname, exc)
        buf.seek(0)
        return buf

    # ---- next for machine (desktop agent polling) ----

    def next_for_machine(self, machine_id: str) -> dict[str, Any] | None:
        # 1. The poll IS the agent's idle-phase liveness signal.  ZADD
        # on every poll keeps the machine in machines:alive without a
        # separate /machines/{id}/heartbeat call.  During a render the
        # agent isn't polling -- liveness flows through the per-job
        # heartbeat (Redis job:{id}:hb) instead.
        self._machine_hb.record(machine_id)

        # 2. Self-heal: a polling agent is by definition alive AND not
        # rendering, so the machine row should say 'available'.  Two
        # ways the row drifts off 'available' and we have to repair:
        #
        #   (a) 'idle'        -- demote_ghosts swept the row before the
        #                        agent's first poll wrote machines:alive
        #                        post-set_available.  Just flip back.
        #   (b) 'processing'  -- a previous render's failure/cancel
        #                        path didn't reach the
        #                        release-machine step (Cloud Run
        #                        deploy mid-flight, idempotent
        #                        already-terminal cancel that
        #                        skipped the pipeline, etc.).  May
        #                        also have left a zombie running job
        #                        assigned to this machine; reclaim
        #                        it via handle_community_machine_idle
        #                        so retry can fire, then flip the
        #                        row back to 'available'.
        #
        # Read Redis first; fall back to PG if the cache is missing
        # or unavailable.
        cached = self._machine_hb.get_status(machine_id)
        if cached is None:
            cached = self._machines.get_status(machine_id)
        if cached and cached != "available":
            if cached == "processing":
                self._community_idle_notifier(machine_id)
            self._state_writer.set_status(machine_id, "available")

        # 3. Try to claim a pending job for this machine.
        job = self._jobs.claim_next_for_machine(machine_id)
        if job is not None:
            # 4. Lock the machine -- allocator stops returning it as
            # available for the next dispatch.  Write goes through the
            # state writer so PG and Redis stay in sync.  Released by
            # the lifecycle on success/failure/cancel; auto-demoted by
            # the stale-sweep if the agent crashes.
            self._state_writer.set_status(machine_id, "processing")
        return job

