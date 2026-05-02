"""Execution machinery for the job-termination package.

The parent package keeps only the **rules** -- the fluent builder and
the two pre-built pipeline constants.  Everything that **runs** the
pipelines lives here:

* ``termination_context``   -- per-call data passed to each step
* ``job_terminator``        -- the executor; iterates a pipeline and
                                calls ``step.run(ctx)``
* ``render_canceler``       -- group-level multi-pass cancel; reuses
                                the per-job step classes

Step classes themselves live one level up at ``../steps/``.  Auto-retry
dispatch delegates to the sibling ``lifecycle_job_retry`` package via
``TryRetryStep``.

External callers don't import from this sub-package directly; the
parent ``lifecycle_job_termination/__init__.py`` re-exports the public
surface.
"""
