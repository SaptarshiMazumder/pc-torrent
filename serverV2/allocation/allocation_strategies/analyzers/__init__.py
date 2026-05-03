"""Analyzers — pure heuristic modules that compute heaviness/cost/time
estimates from a render group's analysis snapshot.

Each analyzer is a pure module: no I/O, no DB, no network.  Inputs are
data contracts (heaviness dict, target render_speed, target price_per_hour);
outputs are scalars or small dicts.  The cost-aware allocators (Economy,
Standard, future Premium) compose these analyzers to make picking
decisions.
"""
