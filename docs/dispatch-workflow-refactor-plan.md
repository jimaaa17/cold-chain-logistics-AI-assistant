# Plan: split `BE/agent/workflow.py` into a layered `agent/dispatch/` package

Status: Phases 0–3 and 5 done on `refactor/dispatch-package`. Phase 4 (optional tidy-ups) not started. Reference design: Gen AI `BE/app/agents/rag_agent/`
(topology-only `graph.py`, one module per node, dependencies through `Runtime[ContextSchema]`,
public input/output schemas).

## Why

`workflow.py` (608 lines) holds six concerns in one file: state and reducers, `DispatchResources`,
twelve nodes plus three routers, prompt text, graph wiring, and the streaming adapter. The nodes are
closures inside `build_dispatch_workflow`, capturing `cfg`, `res`, `now_fn` and `reasoner_system`, so:

- a single node can't be imported or tested on its own; every test builds and streams the whole graph;
- you can't see the flow without scrolling past ~280 lines of node bodies;
- the graph's public contract (what callers send in and get out) is the whole internal state.

What stays as it is, because it is already better than the reference: lazy, thread-safe
`DispatchResources` with `warm()`, no import-time side effects, rules and verification in plain code,
the verifier retry loop and the `interrupt()` approval gate.

## Constraints (things that must not break)

| Caller | Depends on |
|---|---|
| `BE/api/server.py:376,487,519,536` | `agent.workflow.build_dispatch_workflow()`, `iter_dispatch_events`, `pending_approval`, `graph.dispatch_resources.warm()` |
| `BE/agent/cli.py:67,125` | `build_dispatch_workflow(settings)`, `iter_dispatch_events(..., resume=...)` |
| `tests/test_fe_api.py:193,229,248` | patches the **name** `agent.workflow.build_dispatch_workflow` |
| `tests/test_dispatch_workflow.py:17,110-123` | `DispatchResources(settings, llm=, planner_llm=, engine=, http_client=, retrieval_service=)`, `build_dispatch_workflow(settings, resources=, clock=)` |

So `agent/workflow.py` stays as a thin facade that re-exports these names, and
`build_dispatch_workflow`'s signature does not change. Safety net: 36 tests in
`test_dispatch_workflow.py` and 39 in `test_fe_api.py`, all offline.

```bash
python -m unittest discover -s tests -v
```

## Target layout

```
BE/agent/
  workflow.py              facade: re-exports the public names below (kept for server, cli, tests)
  dispatch/
    __init__.py            public API: build_dispatch_workflow, iter_dispatch_events,
                           pending_approval, collect_final, DispatchResources
    state.py               DispatchState, DispatchInput, DispatchOutput, _collect reducer
    context.py             DispatchContext (settings, resources, clock, prompts), DispatchResources
    events.py              emit() and the event shapes (status, tool_input, tool_output, evidence,
                           draft_start, verification, final_answer) as TypedDicts
    prompts.py             load_prompt(); PLANNER_PROMPT and CLARIFY_TEXT read from prompts/*.txt
    text.py                _text, _plain, history()
    nodes/
      intake.py            intake, route_after_intake, clarify
      planner.py           planner, route_after_planner
      gather.py            gather_telemetry, gather_weather, gather_sop (+ one shared _gather helper)
      ledger.py            ledger, rules
      reasoning.py         reasoner, verifier, route_after_verifier
      approval.py          action_gate
      finalize.py          finalize, render_final
    graph.py               build_dispatch_workflow: add_node / add_edge only
    streaming.py           iter_dispatch_events, pending_approval, collect_final
  prompts/
    planner_prompt.txt     (new, moved out of the Python string)
    clarify_text.txt       (new)
    reasoner_prompt.txt    (existing)
```

The subpackage is named `dispatch/` so it doesn't collide with the legacy ReAct `agent/graph.py`
(`AgentOrchestrator`), which the CLI fallback and `test_agent_and_multimodal_rag.py` still use.
The node module is `ledger.py`, not `evidence.py`, to avoid confusion with `agent/evidence.py`.

## Phases

Each phase is one commit and leaves the suite green. Phases 1 and 2 change no behaviour.

### Phase 0: prepare

- There are 26 uncommitted modified files in the repo (including `agent/graph.py`, `api/server.py`,
  `agent/config.py`). Commit or stash them first so the refactor diff is pure.
- Branch `refactor/dispatch-package`; run the suite and record the baseline.

### Phase 1: move the non-node code (mechanical)

1. Create `dispatch/state.py`, `text.py`, `events.py` (`_emit` → `emit`), `prompts.py`,
   `streaming.py`, and `nodes/finalize.py` (`render_final` only, for now). Cut and paste; no edits.
2. Move `DispatchResources` to `dispatch/context.py`.
3. Move `PLANNER_PROMPT` and `CLARIFY_TEXT` into `prompts/*.txt`, loaded by `prompts.py` with the
   current strings as fallbacks (same pattern as `_load_prompt`).
4. `workflow.py` keeps `build_dispatch_workflow` (still with closures) and re-exports everything else.

Done when `workflow.py` holds only the builder and the suite passes untouched.

### Phase 2: nodes as module functions with injected context (the core change)

1. Define the context:

   ```python
   @dataclass(frozen=True)
   class DispatchContext:
       settings: AgentSettings
       resources: DispatchResources
       clock: Callable[[], datetime]
       reasoner_system: str
   ```

2. Turn each closure into a top-level function in `nodes/`, replacing captured variables:
   `cfg` → `runtime.context.settings`, `res` → `runtime.context.resources`,
   `now_fn()` → `runtime.context.clock()`.

   ```python
   def ledger(state: DispatchState, runtime: Runtime[DispatchContext]) -> dict:
       now = runtime.context.clock()
       window = runtime.context.settings.evidence_freshness_hours
       ...
   ```

   Routers that read settings (`route_after_intake` uses `area_half_width_deg` and `sql_max_rows`)
   take `runtime` the same way.
3. `dispatch/graph.py` becomes topology only: `StateGraph(DispatchState, context_schema=DispatchContext)`,
   then the existing `add_node` / `add_edge` lines. It builds the `DispatchContext`, attaches it as
   `graph.dispatch_context`, and keeps `graph.dispatch_resources` for `prewarm_agent`.
4. `iter_dispatch_events` passes `context=graph.dispatch_context` to `graph.stream(...)` for both
   new turns and `Command(resume=...)`. It is the only place the graph is streamed (server, CLI and
   tests all go through it), so this is the single wiring point.
5. Fix in passing: `action_gate` stamps `datetime.now(timezone.utc)` instead of the injected clock.
   Switch it to `runtime.context.clock()`. This is the one deliberate behaviour change; call it out
   in the commit message.
6. New per-node tests that don't build the graph, with `Runtime(context=DispatchContext(...))`
   (LangGraph 1.2.11's `Runtime` is a plain dataclass): `ledger` ordering and numbering,
   `rules` event payload, `route_after_intake` sends for each intent, `route_after_planner` tool-name
   mapping, `route_after_verifier` retry cut-off.

Risk: anything that calls `graph.stream` / `graph.invoke` directly, without going through
`iter_dispatch_events`, gets `runtime.context = None`. Today nothing does; add a test that fails if
`iter_dispatch_events` stops passing the context.

### Phase 3: public input and output schemas

```python
class DispatchInput(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

class DispatchOutput(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    final: str
    verification: dict | None
    approval: dict | None
```

Pass `input_schema=DispatchInput, output_schema=DispatchOutput`. `intake` already resets every
internal field, and `pending_approval` reads `snapshot.tasks`, not values, so callers aren't affected.

### Phase 4: tidy-ups borrowed from the reference (each optional, each its own commit)

- **One gather helper.** The three gather nodes repeat emit `tool_input` → try → map errors into
  `result["error"]` → emit `tool_output`. Extract `_gather(name, args, source, call, on_error)` so each
  node is ~10 lines. Keep the "errors become ledger gaps" behaviour exactly.
- **Move the LLM factory out of the legacy graph.** `DispatchResources` imports `build_reasoner_llm`
  and `build_default_tools` from `agent/graph.py` (the old ReAct agent). Move them to `agent/llm.py`;
  the old graph imports from there. Add a small `LLMTask` table (`PLANNER`, `REASONER`) only once
  the two need different models or temperatures.
- **Retry policies on the LLM nodes.** `planner` and `reasoner` currently fail the turn on a transient
  provider error. `add_node("reasoner", reasoner, retry_policy=RetryPolicy(max_attempts=2))` is the
  reference's approach. This changes behaviour (latency on failure), so decide explicitly.

### Phase 5: documentation

- `CLAUDE.md`, "agent/: agentic dispatch workflow": point to `dispatch/` and its modules.
- `BE/AGENTS.md` §1: add the boundary rule, "dispatch nodes get the LLM, engine, HTTP and retrieval
  from `runtime.context`, never by building or importing clients themselves".

## Not copying from the reference

- Building every client at startup in `main.py` (cold-chain's lazy `DispatchResources` + `warm()` is better).
- `Any`-typed context fields: `DispatchContext` is fully typed.
- Prompt modules that read config at import time.
- Routing on string literals when an enum exists: keep using `Intent.kind` values from `intent.py`.

## Out of scope (separate follow-up)

`BE/api/server.py` (931 lines) mixes SSE framing, audit export, config-error handling, the offline
fallback, approval, purge, and two server implementations. Same treatment later: `api/sse.py`,
`api/audit.py`, `api/routes/*.py`, `api/stdlib_server.py`.
