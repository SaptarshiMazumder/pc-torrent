"""Gateway clients -- neutral, layer-agnostic entry points into a module's
facade.

A client lives here (not inside the calling layer) so any caller can depend
on it without reaching across domains.  Currently: ``AllocationClient`` (the
sole permitted importer of ``AllocationFacade``).
"""
