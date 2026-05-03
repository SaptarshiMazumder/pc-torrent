"""Helpers used by the allocation strategies — pure-function math
(power scoring, frame distribution, budget limiting, heaviness banding)
plus value objects (chunk request, tier constants).

Strategies depend on these; nothing depends on strategies.  Kept in
its own subfolder so the strategies/ folder is the strategies, not
the math under them.
"""
