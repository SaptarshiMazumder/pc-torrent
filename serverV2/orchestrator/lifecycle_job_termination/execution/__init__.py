"""Execution machinery for the job-termination package.

The parent package keeps only the **rules** -- the fluent builder and
the two pre-built pipeline constants.  Everything that **runs** the
pipelines lives here:

* ``termination_context``   -- per-call data passed to each step
* ``retry_dispatcher``      -- the auto-retry decision (extracted from
                                RenderLifecycle._try_dispatch_retry)
* ``steps/``                -- one file per step class; each does one
                                action and reads/writes the context
* ``job_terminator``        -- the executor; iterates a pipeline and
                                calls ``step.run(ctx)``
* ``render_canceler``       -- group-level multi-pass cancel; reuses
                                the per-job step classes

External callers don't import from this sub-package directly; the
parent ``lifecycle_job_termination/__init__.py`` re-exports the public
surface.
"""
