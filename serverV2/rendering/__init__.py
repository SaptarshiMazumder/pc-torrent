"""rendering -- the render-pipeline bounded context (clean-architecture slice).

Layered inside: ``domain`` (pure rules) <- ``application`` (use-cases +
ports) <- ``presentation`` / ``infrastructure`` (adapters).  Depends inward
on the shared kernel (``serverV2.core``); reaches the billing and identity
contexts only through ports (forward) and events (reverse).

DORMANT: nothing here is imported by the running app yet.  It is a parallel
skeleton built alongside the live code (``services/``, ``orchestrator/``,
``api/``) and is wired in later, one use-case at a time, via ``bootstrap``.
See ``rendering/README.md``.
"""
