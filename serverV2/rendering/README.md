# rendering — the render-pipeline bounded context (walking skeleton)

The render pipeline modelled as **one bounded context**, organised
**concept-first**: `rendering/` contains one folder per concept
(`render_group`, `render_group_chunk`, `allocation`, `fleet`), and each
concept holds its own 4 clean-architecture layers. Read a concept folder
top-to-bottom to see that concept whole.

**Parallel & dormant** — not imported by the running app. Live code still
lives in `services/`, `orchestrator/`, `api/`. We flesh out and wire one
concept at a time; the top-level composition happens last (strangler-fig).

Vision map: `umls/rendering_concept_layers.puml`.

## Layout

```
render_group/                     ← the coordinator concept (COMPLETE skeleton)
├── domain/          render_group · group_status_policy · terminal_cost_rollup
├── application/     create · confirm_upload · cancel · delete · reconcile ·
│   │               get_status · list · get_outputs · estimate_cost · on_credits_exhausted
│   └── ports/       the 9 interfaces render_group needs (consumer-owned):
│                      *_repository  render_group · chunk · output_frame
│                      *_gateway     storage · group_status
│                      *_service     allocation · fleet (siblings) · credits · user (external)
├── presentation/   render_groups_router · render_group_presenter
├── infrastructure/ render_group_repository · group_status_gateway · storage_gateway (own)
│                   + credits_service_adapter · user_service_adapter (delegate to external contexts)
└── bootstrap.py    build_render_group — wires this concept; siblings injected as params

render_group_chunk/               ← PARTIAL (relocated; fleshed out in its own step)
├── application/    success_handler · failure_handler · retry_handler
└── infrastructure/ chunk_repository · output_frame_repository  (implement render_group's ports)

allocation/  └ infrastructure/  allocation_service_adapter        ← PARTIAL
fleet/       └ infrastructure/  fleet_service_adapter             ← PARTIAL
```

`identity/` and `billing/` are **separate top-level contexts** (their own 4
layers), reached from render_group via `IUserService` / `ICreditsService`
(forward) and domain events (reverse).

## Rules the skeleton encodes
- **Concept-first, layer-within:** `rendering/render_group/domain/…`, not `rendering/domain/render_group/…`.
- **Dependency rule** inside each concept: presentation / infrastructure → application → domain. Domain imports nothing outward (only the shared kernel `serverV2.core`).
- **Consumer-owned ports:** `render_group` owns every interface it needs; siblings *implement* them and are injected. The graph stays acyclic — `render_group_chunk → render_group` (callbacks call reconcile, infra implements its ports), never the reverse.
- **Same mechanism for siblings and externals:** render_group reaches allocation, fleet, billing, identity all through a port + an adapter + bootstrap wiring; only the adapter's delegation target differs.

## Status
Ports are **fully written** (the contracts). Every use-case is a real class
with its ports constructor-injected and a documented `NotImplementedError`
body. `render_group/bootstrap.py` **wires the concept for real** (construction
runs no I/O). No business logic ported yet — that's the next step, per concept.

**Parity gate:** `render_group/domain/group_status_policy.compute_group_status`
must be proven equal to the live `callbacks/group_status_aggregator` (table
test) before it is trusted.
