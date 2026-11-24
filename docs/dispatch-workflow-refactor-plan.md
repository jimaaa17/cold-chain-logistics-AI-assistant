# Plan: split `BE/agent/workflow.py` into a layered `agent/dispatch/` package

Status: Phases 0–3 and 5 done on `refactor/dispatch-package`. Phase 4: LLM factory move (`35ef48c`) and prompt templates Step 1 (`b26bc39`) and Step 2 (`b8561ad`) done; gather helper and retry policies not started (see the notes on each). Phase 6 (one failure policy) and Phase 7 (model routing, thresholds and verification within coded guardrails; all modes default to `code`) done, with the changes listed under each. Reference design: Gen AI `BE/app/agents/rag_agent/`
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
`DispatchResources` with `warm()`, no import-time side effects, the verifier retry loop and the `interrupt()` approval gate. (Phase 7 lets
a model take part in routing, thresholds and verification; the code versions stay as the default, floor and fallback.)

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

- **Prompts as LangChain `ChatPromptTemplate`, written like Gen AI's `prompts/intent.py`.**
  Reference: `Gen AI/BE/app/agents/rag_agent/prompts/intent.py` and its node `nodes/intent.py`.

  **What the reference does, and what we take from it**

  | `intent.py` pattern | Take it? | Cold-chain use |
  |---|---|---|
  | One module per step, exporting one `ChatPromptTemplate` (`intent_classification_prompt`) | Yes | `dispatch/prompts/{planner,reasoner}.py` |
  | `from_messages([("system", ...), MessagesPlaceholder(...), ("human", ...)])` | Yes | Same three parts for planner and reasoner |
  | System text split into XML sections: `<system_context>`, `<intent_types>`, `<classification_rules>`, `<reasoning_guidelines>`, `<output_format>`, `<examples>`, `<critical_requirements>` | Yes | Rewrite the reasoner and planner text into tagged sections (drafts below) |
  | Ordered rules: "Apply these checks in order - STOP at first match", `PRIORITY 1/2/3` | Yes, for the planner | Planner decides which tools to call in a fixed priority order |
  | Runtime facts as variables in the system text (`Current scope: {filter_type} = '{filter_name}'`) | Yes | Freshness window, row limit and `as_of` instead of hard-coded "24 hours" / "10 rows" |
  | Human turn as labelled fields (`Current query to classify: {query}`, `Filter type: ...`, `Preselected documents provided: ...`) | Yes | Pass the code-computed `Intent` fields (place, coordinates, temperature filter) to the planner |
  | Few-shot `<examples>` that include the expected output | Yes | One or two per prompt, using made-up evidence ids |
  | Literal JSON braces escaped as `{{ }}` | Yes, as a rule | Only if a prompt shows JSON; none do today |
  | `MessagesPlaceholder("messages", n_messages=...)` to cap history | Yes, without config | `n_messages=HISTORY_MESSAGES` (a module constant, 4 as today). `text.history()` still drops the current question |
  | Pydantic model + `with_structured_output(..., method="json_mode", include_raw=True)` + safe default on error | Not now | The planner already returns typed tool calls through `bind_tools`, and the reasoner must return Markdown with `ACTION[...]` tags for the verifier. Use this pattern only if an LLM step ever has to return a fixed shape |
  | `_config = Config()` at import time, to read `n_messages` | No | Settings reach prompts as variables at render time, never at import |
  | `<critical_requirements>` asks for a confidence score that `IntentClassification` has no field for | No | Every instruction must match the output the node actually parses |
  | Intent decided by an LLM | No | Cold-chain routes in code (`agent/intent.py`, regex + gazetteer), which is faster, free and testable. Keep it; Phase 7 adds an opt-in model only for questions code can't place |

  **Step 1: port as-is (no prompt wording change)**. *Done in `b26bc39`; messages captured before and after across four scenarios were byte-identical.*

  - Turn `dispatch/prompts.py` into a package: `prompts/__init__.py`, `planner.py`, `reasoner.py`,
    `clarify.py` (the clarify reply stays a plain `CLARIFY_TEXT` constant, since no LLM is called).
  - Wrap today's text unchanged in `ChatPromptTemplate`s, render them in the nodes with
    `format_messages(...)` and call `llm.invoke(msgs)` as now (reasoner token streaming isn't affected).
  - The reasoner's optional sections become helpers, `gaps_block(gaps)` and `retry_block(problems)`,
    each returning `""` or a block. That is the same trick Gen AI uses for its conversation summary.
  - `DispatchContext` holds `reasoner_prompt` / `planner_prompt: ChatPromptTemplate` instead of the
    `*_system` strings, so tests can inject a template. `ChatPromptTemplate` is a mutable pydantic
    model, so each field needs `field(default_factory=...)`.
  - Delete `reasoner_prompt.txt`, `planner_prompt.txt` and `clarify_text.txt`. Keep
    `system_prompt.txt`: the legacy ReAct agent and `test_sop_rules.py` still read it.
  - Done when the rendered messages are byte-identical to today's (compare once before deleting the files).

  **Step 2: restructure the wording in the `intent.py` style (deliberate prompt change)**. *Done in `b8561ad`.*

  Review of the drafts below. These were fixed in the implementation; the drafts are kept as written:
  - The reasoner example used `ACTION[escalate_qa]`, which isn't an action in `rules.py`. A model
    copying it would produce a blocked action. A temperature breach permits `restart_cooling`, so the
    example uses that.
  - `{as_of}` would have rendered as a raw ISO string (`2026-11-18T22:00:00+00:00 UTC`) and repeats the
    ledger header (`EVIDENCE LEDGER (now ... UTC; fresh = newer than 24 h)`). Dropped; only
    `freshness_hours` and `max_rows` are variables.
  - The planner's PRIORITY 1 (`SELECT TOP {max_rows}`, `ORDER BY Timestamp DESC`, always) and its
    "average per risk class" example would turn aggregate questions into raw rows. The reasoner can't
    compute averages (rule 1), so aggregates must come from SQL. The live baseline showed Gemini already
    writing `GROUP BY` + `MAX(Timestamp) AS Timestamp`. The rule now splits aggregates (COUNT/AVG/MIN/MAX,
    `GROUP BY`, `MAX(Timestamp) AS Timestamp`) from individual readings (`TOP n`, `ORDER BY Timestamp DESC`).
  - `describe_temp_filter` didn't exist. `intent_variables()` in `prompts/planner.py` renders place,
    coordinates, an area filter and a temperature filter as SQL conditions. The area bounds come from
    `intent.area_sql`, shared with `build_scan_sql`, so open questions and incident scans use the same box.
  - Added to the reasoner: SQL-aggregated columns (`Avg_*`, `Shipment_Count`) are ledger values, and
    aggregate rows get their own table layout. Before this, the reasoner wrote "no averages can be
    calculated" next to the averages.

  Live check (5 questions, Gemini, telemetry view): every draft passed verification on the first attempt,
  both before and after. "How many High Risk shipments are near Long Beach?" went from 10 raw rows plus an
  unneeded SOP search to one `COUNT(*)` within the scan area. One run per question, so this shows no
  regression, not a measured improvement.

  Draft `dispatch/prompts/reasoner.py`. The rules keep their numbers and wording, because the
  verifier (`agent/verifier.py`) enforces rules 1–5 and the `ACTION[...] on [T3]:` line format:

  ```python
  from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

  HISTORY_MESSAGES = 4

  REASONER_SYSTEM = """<system_context>
  You are the dispatch analyst for a cold-chain logistics control tower. Operators ask about
  refrigerated shipments, port congestion and compliance with the Standard Operating Procedure (SOP).
  Evidence time: {as_of} UTC. A reading is "fresh" if it is at most {freshness_hours} hours old.
  </system_context>

  <evidence_ledger>
  The ledger is built by code from the telemetry database, measured weather and the SOP. Thresholds
  are already evaluated in code: SOP TRIGGERS lists breaches on fresh evidence, HISTORICAL OR UNDATED
  BREACHES lists the rest. Your job is to judge what matters most, explain it clearly and propose only
  the actions the triggers allow.
  </evidence_ledger>

  <rules>
  Your answer is checked automatically against the ledger and rejected if it breaks rules 1-5.
  1. Use only values that appear in the ledger. Do not compute averages, sums, percentages or any new numbers. Do not estimate.
  2. Cite the evidence id in square brackets after every reading or fact, for example [T3] or [W1]. Cite SOP excerpts as [S1].
  3. Freshness: a record marked "historical" or "undated" is not a current shipment. Never call it active or live. Say plainly that the data is historical and give its timestamp.
  4. Actions: propose an action only if it is listed under SOP TRIGGERS for that evidence id. Write each one on its own line, exactly as:
     ACTION[action_id] on [T3]: one sentence explaining why, citing the SOP section.
     Never propose actions for historical breaches. If there are no triggers, write "No SOP action is required on current data."
  5. Every SOP trigger must be reported, with its evidence id.
  6. If a temperature breach is fresh, you may add (without an ACTION line) that the SOP allows diversion to emergency cold storage only if the ETA delay exceeds 1 hour, which the data does not show.
  7. Weather is context only. The SOP has no weather threshold, so weather never justifies a diversion. Port congestion comes only from Port_Congestion_Level in telemetry.
  8. Write plain Markdown. No emoji. Keep it short: an operator reads this under time pressure.
  </rules>

  <output_format>
  ## Summary
  Two or three sentences: what the data shows and whether any SOP action applies now.

  ## Readings
  A Markdown table of the telemetry records (at most {max_rows} rows): Evidence | Time (UTC) | Lat, Lon | Temp °C | Risk | Delay prob | Port congestion | Freshness. Leave a blank line before and after the table. Omit this section if there is no telemetry.

  ## Weather
  One line from the weather record, if present, with its id.

  ## SOP assessment
  Breaches on fresh data first (with ACTION lines), then historical breaches as history. End with the SOP citation(s) used.
  </output_format>

  <examples>
  <example>
  SOP TRIGGERS: [T1] temperature 6.8 °C > 4.0 °C, actions: escalate_qa
  Answer line: ACTION[escalate_qa] on [T1]: cargo is at 6.8 °C [T1], above the 4.0 °C limit in SOP §1 [S1].
  </example>
  <example>
  SOP TRIGGERS: none. HISTORICAL OR UNDATED BREACHES: [T2] 7.1 °C at 2023-07-13 22:00
  Answer line: No SOP action is required on current data. [T2] is historical (2023-07-13 22:00 UTC), not a live shipment.
  </example>
  </examples>

  <critical_requirements>
  - Every number has an evidence id; no number is computed.
  - ACTION lines only for SOP TRIGGERS, in the exact format of rule 4.
  - Historical data is never described as current.
  </critical_requirements>"""

  REASONER_HUMAN = (
      "<question>\n{question}\n</question>\n\n"
      "<evidence_ledger_data>\n{ledger}\n</evidence_ledger_data>"
      "{gaps_block}{retry_block}"
  )

  reasoner_prompt = ChatPromptTemplate.from_messages([
      ("system", REASONER_SYSTEM),
      MessagesPlaceholder("history", optional=True, n_messages=HISTORY_MESSAGES),
      ("human", REASONER_HUMAN),
  ])

  def gaps_block(gaps: list[str]) -> str:
      if not gaps:
          return ""
      return ("\n\n<data_gaps>\nSay these sources were unavailable; do not guess.\n"
              + "\n".join(f"- {g}" for g in gaps) + "\n</data_gaps>")

  def retry_block(problems: list[str]) -> str:
      if not problems:
          return ""
      return ("\n\n<verifier_feedback>\nYour previous draft was rejected by the automatic check. "
              "Rewrite it and fix exactly these problems:\n"
              + "\n".join(f"- {p}" for p in problems) + "\n</verifier_feedback>")
  ```

  Draft `dispatch/prompts/planner.py`. The planner only runs for `open` questions, and its human
  turn carries the fields `intake` already extracted, like Gen AI passes `filter_type` and `has_preselected`:

  ```python
  PLANNER_SYSTEM = """<system_context>
  You plan data retrieval for a cold-chain control tower. Call the tools needed to answer the
  question, in parallel where possible. Do not answer the question yourself; another step does that.
  </system_context>

  <tools>
  - query_telemetry_db: one read-only T-SQL SELECT against FDE_VIEWS.VW_ACTIVE_FLEET.
    Columns: Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code,
    Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index.
  - fetch_corridor_conditions: measured and forecast weather for a latitude/longitude.
  - search_compliance_sop: search the SOP knowledge base.
  </tools>

  <planning_rules>
  Apply these checks in order. Each one that matches adds a tool call:
  PRIORITY 1: the question asks about shipments, temperatures, risk, delay or congestion
    → query_telemetry_db. Use SELECT TOP {max_rows}, always include Timestamp, ORDER BY Timestamp DESC.
  PRIORITY 2: a place or coordinates are known (see the human turn)
    → fetch_corridor_conditions for those coordinates.
  PRIORITY 3: the question asks what to do, or about a threshold, rule or procedure
    → search_compliance_sop with a short query in the SOP's words.
  If nothing matches, call search_compliance_sop with the question.
  </planning_rules>

  <examples>
  <example>
  Question: "What is the average temperature per risk class near Long Beach?"
  Calls: query_telemetry_db(SELECT TOP {max_rows} Timestamp, Risk_Classification, Current_Temperature_C ... ORDER BY Timestamp DESC),
         fetch_corridor_conditions(latitude=33.77, longitude=-118.19)
  </example>
  </examples>

  <critical_requirements>
  - SELECT only, against FDE_VIEWS.VW_ACTIVE_FLEET only.
  - Never answer in text; only call tools.
  </critical_requirements>"""

  PLANNER_HUMAN = (
      "Question to plan for: {question}\n\n"
      "Known place: {place}\nCoordinates: {coordinates}\n"
      "Temperature filter: {temperature_filter}\nRouting reason: {intent_reason}"
  )

  planner_prompt = ChatPromptTemplate.from_messages([
      ("system", PLANNER_SYSTEM),
      MessagesPlaceholder("history", optional=True, n_messages=HISTORY_MESSAGES),
      ("human", PLANNER_HUMAN),
  ])
  ```

  The nodes fill the variables from the context and state. Settings are read at render time, not import time:

  ```python
  # reasoner node
  msgs = ctx.reasoner_prompt.format_messages(
      as_of=state["as_of"], freshness_hours=ctx.settings.evidence_freshness_hours,
      max_rows=ctx.settings.sql_max_rows, history=history(state),
      question=state["question"], ledger=ledger_text,
      gaps_block=gaps_block(state.get("gaps") or []),
      retry_block=retry_block(previous.get("problems") or []) if attempt > 1 else "",
  )

  # planner node
  intent = Intent(**state["intent"])
  msgs = ctx.planner_prompt.format_messages(
      max_rows=ctx.settings.sql_max_rows, history=history(state), question=state["question"],
      place=intent.place or "none",
      coordinates=f"{intent.latitude}, {intent.longitude}" if intent.has_area else "none",
      temperature_filter=describe_temp_filter(intent),  # e.g. "> 4 °C", or "none"
      intent_reason=intent.reason,
  )
  ```

  Notes for Step 2:
  - It changes what the model sees, so it is its own commit, after Step 1. The offline tests use fake
    LLMs, so they show the wiring works but not the answer quality. Before merging, run 5–10 real questions
    (incident scan near Long Beach, historical-only data, SOP lookup, an aggregate, a retry case)
    through `python -m agent` on both versions and compare: verifier pass rate on the first attempt,
    `ACTION` lines and citations.
  - The example evidence values (6.8 °C, [T1], [T2]) are illustrative. Check that the verifier doesn't
    match numbers from the system prompt. It reads only the draft, so a model copying an example
    number without a ledger id would fail rule 1, which is the behaviour we want.
  - Literal braces: none in these drafts. If a JSON example is ever added, double them (`{{ }}`) as `intent.py` does.

  **Tests** (`tests/test_dispatch_prompts.py`):
  - the template input variables equal the set each node passes (catches a stray brace or a missing variable);
  - the messages the planner and reasoner send, captured with a recording fake LLM, including `history` trimmed to `HISTORY_MESSAGES`;
  - `<data_gaps>` and `<verifier_feedback>` appear only when there are gaps or a retry;
  - injecting a different template through `DispatchContext`.

  Also update `tests/test_dispatch_nodes.py`, which asserts on `ctx.reasoner_system`, and the `prompts` lines in `CLAUDE.md`.

- **One gather helper.** The three gather nodes repeat emit `tool_input` → try → map errors into
  `result["error"]` → emit `tool_output`. Extract `_gather(name, args, source, call, on_error)` so each
  node is ~10 lines. Keep the "errors become ledger gaps" behaviour exactly.
  *Review: recommend skipping.* Each node catches different exceptions and writes different gap and
  tool-output text, and `gather_sop` records a gap on an empty result without any exception. The helper
  would need callbacks for all of that to save about 15 lines per node, and it puts the gap strings that
  tests and operators rely on at risk.
- **Move the LLM factory out of the legacy graph.** *(Done in `35ef48c`.)* `DispatchResources` imports `build_reasoner_llm`
  and `build_default_tools` from `agent/graph.py` (the old ReAct agent). Move them to `agent/llm.py`;
  the old graph imports from there. Add a small `LLMTask` table (`PLANNER`, `REASONER`) only once
  the two need different models or temperatures.
- **Retry policies on the LLM nodes.** *(Superseded by Phase 6, which adopts the transient-only `retry_on` below.)* `planner` and `reasoner` currently fail the turn on a transient
  provider error. `add_node("reasoner", reasoner, retry_policy=RetryPolicy(max_attempts=2))` is the
  reference's approach. This changes behaviour (latency on failure), so decide explicitly.
  *Review: don't use the default `retry_on`.* LangGraph 1.2.11's `default_retry_on` retries every
  exception type it doesn't recognise, which includes provider auth and quota errors, so a permanent
  failure takes twice as long. The provider clients also retry internally. If adopted, pass a `retry_on`
  that accepts only 429/5xx/timeouts (reuse `rag.core.resilience`'s transient check). A retried
  reasoner re-emits `draft_start`, which clears the draft in the FE, so streamed tokens don't double.

### Phase 5: documentation

- `CLAUDE.md`, "agent/: agentic dispatch workflow": point to `dispatch/` and its modules.
- `BE/AGENTS.md` §1: add the boundary rule, "dispatch nodes get the LLM, engine, HTTP and retrieval
  from `runtime.context`, never by building or importing clients themselves".

### Phase 6: one failure policy for data gathering and model steps

Goal: no single failing source or model call ends a turn. Every failure becomes a recorded **gap**,
the step falls back to a known-safe behaviour, and the operator sees which part was degraded.
This comes before Phase 7, because Phase 7's code fallbacks are exactly what this phase wires in.

*Done. What differs from the text below:*
- The transient retry is inside `run_model_step`. A node `RetryPolicy` never sees an exception the step has handled.
- `degraded` is its own audited event type; `status` events are not audited. Data-source outages emit it too.
- `gaps` has an append reducer. The ledger used to overwrite gaps recorded before it.
- Clients and prompts are built outside the wrapper, so a missing API key is still an error frame.
- The evidence-only answer skips the verifier and its retry, and sends `draft_start` again to clear streamed text.
- Not built: the optional second model for content filtering.

**The contract (same for every step)**

| Outcome | What the step returns | Visible to |
|---|---|---|
| Success | its normal output | — |
| Empty but valid ("no rows", "no SOP match") | its normal output, marked `status: "empty"` | ledger note, not a gap |
| Invalid input (bad SQL, missing coordinates, unknown tool) | `status: "error"`, no data | gap: `"<step>: <reason>"` |
| Dependency down (DB, weather API, Pinecone, model provider) | `status: "error"`, no data, or the step's fallback output | gap + `degraded` event |

- `gathered` items get a `status: "ok" | "empty" | "error"` field. `ledger` turns only `error` into a gap.
  Today an empty SOP search is a gap while empty telemetry isn't; this makes them consistent.
- One `gaps: list[str]` stays the single list, prefixed by step (`"telemetry: ..."`, `"router: ..."`),
  so the reasoner's `<data_gaps>` block and the final footer cover both data and model failures.
- New event `{"type": "status", "phase": "degraded", "step": "...", "fallback": "...", "reason": "..."}`
  in `events.py`. The FE (`dispatchEvents.js`) and `api/server.py` audit must handle it, since events are a three-way contract.

**Data gathering (tighten what exists)**

- `gather_weather`: missing or out-of-range `latitude`/`longitude` becomes the gap
  `"weather: no valid coordinates"`. Today it silently fetches 0.0, 0.0.
- `route_after_planner`: an unknown tool name becomes a gap (`"planner: unknown tool X"`) instead of being dropped.
- Error text stays type-only (`TimeoutError`, `OperationalError`), never the raw exception, which can
  contain connection details.

**Model steps (new)**

A small helper in `dispatch/resilience.py` wraps every model call:

```python
def run_model_step(step: str, call: Callable[[], T], fallback: Callable[[], T], *,
                   emit: Callable[[dict], None], fallback_name: str) -> tuple[T, list[str]]:
    """Runs `call`; on any model failure returns (fallback(), [gap]) and emits a `degraded` event."""
```

- **Transient** failures (429, 5xx, timeouts) are retried once with LangGraph's
  `RetryPolicy(max_attempts=2, retry_on=is_transient)`, reusing `rag.core.resilience`'s check. This
  replaces the Phase 4 "retry policies" item.
- **Permanent** failures (auth, quota, invalid request), **unparseable structured output**
  (`OutputParserException`, pydantic `ValidationError`) and **empty or content-filtered replies** go
  straight to the fallback, with no retry.
- Optional, as in Gen AI's `get_fallback_llm`: try a second model (an `LLMTask` fallback entry) before
  the code fallback, for content filtering.

| Model step | Fallback when it fails | Gap text |
|---|---|---|
| `route` (Phase 7) | `agent/intent.py` `classify_intent` (today's code router) | `router: model unavailable, routed by keyword rules` |
| `planner` | Code plan: the incident-scan SQL if the intent has an area or temperature filter, plus SOP search with the question | `planner: model unavailable, used the standard scan` |
| `assess` (Phase 7) | `agent/rules.py` `evaluate_rules` (today's coded thresholds) | `assessment: model unavailable, used coded SOP thresholds` |
| `reasoner` | A deterministic answer: the ledger as a table, the triggers and the gaps. No `ACTION` lines, so nothing goes to approval. Marked unverified | `answer: model unavailable, showing evidence only` |
| `judge` (Phase 7) | `agent/verifier.py` `verify_draft` (today's code verifier) | `verification: model unavailable, checked by code rules` |

**Tests**
- Each model step with a fake LLM that raises: transient error (retried once), permanent error,
  invalid structured output, empty reply. Assert fallback output, gap text and `degraded` event.
- A whole-graph run where every model call fails still ends in `final_answer`, with the gaps listed and no approval request.
- `gather_weather` without coordinates; planner with an unknown tool; empty vs failed SOP search.

### Phase 7: the model takes the call on routing, thresholds and verification

Decision: wherever it's safe, the model decides and code checks its work, rather than code deciding
and the model explaining. Today's code paths (`intent.py`, `rules.py`, `verifier.py`) stay, as the
Phase 6 fallbacks and as cheap guardrails.

*Done. What differs from the text below:*
- All modes default to `code`, not `shadow`: shadow pays for both paths on every request.
- Routing asks the model only when code says `open` or `clarify`; the standard scan keeps its latency.
- The coded rules are a floor. The model can add findings only for a (field, direction) they don't cover,
  code does the comparison, the quote must be in the cited excerpt, and added findings carry no actions.
  Checking actions against `ACTION_LABELS` would have let through `divert_emergency_cold_storage` and `write_off`.
- `Finding` has no `observed`, `breached` or `actions`: code reads the ledger and decides those.
- The judge runs only on drafts the code checks passed and can only add problems. The node is still `verifier`.
- Disagreements are an audited `model_decision` event.
- "No extra waiting" for `assess` was wrong: the reasoner needs its triggers. Each mode adds one sequential call.
- Evals on the configured model: 26 of 27. The miss (a reading below 0 °C) is kept by the coded floor.
  Other providers have not been run.

**What changes and what stays in code**

| Decision | Today (code) | After (model) | Code guardrail that stays, and why |
|---|---|---|---|
| Routing | `intent.py` regex + gazetteer | `route` node: model classifies the question and extracts place and filters (structured output) | Named places resolve to coordinates through `GAZETTEER` (models invent coordinates). Coordinates must be in range |
| Thresholds | `rules.py`: 4.0 °C, 0.0 °C, 7.0, 0.65 hard-coded | `assess` node: model reads the SOP excerpts in the ledger and decides which readings breach which SOP section and which actions apply | Action ids must be in `ACTION_LABELS`. Actions only on **fresh** evidence (freshness is timestamp maths in `evidence.py`). The quoted reading must equal the ledger value |
| Verification | `verifier.py`: number matching, citations, wording, coverage | `judge` node: model checks the draft against the ledger and returns pass/fail with problems | Cited ids must exist. `ACTION` tags must match the assessment's permitted actions. High-stakes actions always go to `interrupt()` |
| SQL safety | `validate_readonly_select` + read-only role | unchanged | Never a model call: it guards the database |
| Approval | `interrupt()` for `HIGH_STAKES_ACTIONS` | unchanged | Never a model call: it moves or destroys freight |

The guardrails are deliberately narrow. Each is a lookup or an equality check that can't be wrong, and
each protects something a model mistake would make costly: the database, freight, or a fabricated reading.

**New flow**

```
intake ─ route (model) ─┬─ clarify ───────────────────────────────────────────────┐
                        ├─ gather_* (Send) ─┐                                       │
                        └─ planner ─────────┴─ ledger ─ assess (model) ─ reasoner ─ judge (model) ─┬─ action_gate ─ finalize
                                                                          ▲                         │
                                                                          └──── retry once ─────────┘
```

`intake` keeps resetting per-turn fields. `route_after_intake` becomes `route_after_route`, reading the
model's decision. `rules` is replaced by `assess`, and `verifier` by `judge`.

**Structured outputs** (`dispatch/schemas.py`, pydantic, used with `llm.with_structured_output(...)`)

```python
class RouteDecision(BaseModel):
    kind: Literal["incident_scan", "sop_lookup", "open", "clarify"]
    reasoning: str                      # one or two sentences, logged and audited
    place: str | None = None            # resolved through GAZETTEER when known
    latitude: float | None = None       # only if the user gave coordinates
    longitude: float | None = None
    min_temp_c: float | None = None
    max_temp_c: float | None = None

class Finding(BaseModel):
    evidence: str                       # "T3"
    sop_evidence: str                   # "S2", the excerpt that states the threshold
    sop_section: str                    # "§1 Critical Breach"
    field: str                          # "Current_Temperature_C"
    observed: float                     # must equal the ledger value (guardrail)
    threshold_quoted: str               # the SOP's own words, e.g. "> 4.0 °C"
    breached: bool
    actions: list[str] = []             # ids from ACTION_LABELS (guardrail)

class Assessment(BaseModel):
    findings: list[Finding]
    reasoning: str

class Verdict(BaseModel):
    passed: bool
    problems: list[str]                 # fed back to the reasoner on the retry, as today
```

After the guardrails run, `assess` produces the same `triggers` / `historical` lists the reasoner,
`render_ledger` and `action_gate` already use, so those don't change. Findings on non-fresh evidence
go to `historical` whatever the model said; findings that fail a guardrail are dropped and recorded as
a gap (`"assessment: T3 observed 7.2 ≠ ledger 6.8"`).

**Prompts** (`dispatch/prompts/{router,assessor,judge}.py`), written in the `intent.py` style used for
the planner and reasoner: `<system_context>`, ordered `<rules>`, `<output_format>`, `<examples>`,
`<critical_requirements>`, with settings passed as variables at render time.
- `router`: the four routes in priority order, the gazetteer place names, and examples taken from
  today's `intent.py` tests, so the eval cases already exist.
- `assessor`: "use only thresholds stated in the SOP excerpts and cite the excerpt; if no excerpt
  states a threshold for a field, don't assess it". A missing SOP section is reported as a gap, not
  filled from memory.
- `judge`: today's five verifier checks, written as rules for the model. The code number-matcher's
  unmatched numbers are passed in as hints (`<unmatched_numbers>`), so the model makes the call on
  rounding and aggregated values instead of the regex.

**Models.** This adds up to three model calls per turn (route, assess, judge; incident scans make one
call today). Now is the time for the `LLMTask` table from Phase 4: `ROUTER` and `JUDGE` on a fast,
cheap model at temperature 0; `ASSESS` and `REASONER` on the main model. `JUDGE` should differ from
`REASONER`, so the checker doesn't share the writer's blind spots. Run `assess` and the reasoner's
prompt build with no extra waiting: `assess` only needs the ledger.

**Rollout, one decision at a time, behind settings**

```python
routing_mode: Literal["code", "shadow", "model"] = "shadow"
thresholds_mode: Literal["code", "shadow", "model"] = "shadow"
verification_mode: Literal["code", "shadow", "model"] = "shadow"
```

- `shadow`: run both, use the **code** result, and record disagreements as a `status` event and in
  the audit log and Langfuse.
- Switch a setting to `model` once its disagreement rate on the eval set and in shadow logs is
  acceptable. Order: routing first (lowest risk), then verification, then thresholds.
- `code` keeps today's behaviour exactly. It's also the fallback in every mode (Phase 6).

**Evals** (`tests/evals/dispatch_cases.py`, run on demand against a real model, not in CI):
20–30 questions with expected route, triggers, permitted actions and pass/fail verdicts. Start from
the questions in `test_dispatch_workflow.py`, `test_dispatch_nodes.py` and the `intent.py` tests, and
add hard cases: a historical-only breach, mixed fresh and historical readings, an SOP that doesn't
mention a field, an aggregate answer, a reading exactly at a threshold.

**Offline tests** (fake LLMs returning fixed structured outputs):
- each guardrail: invented coordinates replaced by the gazetteer, an action outside the catalog, an
  action on historical evidence, an `observed` value that doesn't match the ledger, a cited id that
  doesn't exist;
- shadow mode uses the code result and emits a disagreement event;
- `model` mode with the model failing falls back exactly as Phase 6 says.

**Docs.** The "who decides what" text changes in `dispatch/graph.py`'s docstring, `CLAUDE.md` and
`BE/AGENTS.md`: "the model routes, assesses SOP thresholds, writes and checks the answer; code
computes freshness, guards SQL and actions, and a dispatcher approves high-stakes actions".

## Not copying from the reference

- Building every client at startup in `main.py` (cold-chain's lazy `DispatchResources` + `warm()` is better).
- `Any`-typed context fields: `DispatchContext` is fully typed.
- Prompt modules that read config at import time (`intent.py`'s `_config = Config()`).
- Routing on string literals when an enum exists: keep using `Intent.kind` values from `intent.py`.

## Out of scope (separate follow-up)

`BE/api/server.py` (931 lines) mixes SSE framing, audit export, config-error handling, the offline
fallback, approval, purge, and two server implementations. Same treatment later: `api/sse.py`,
`api/audit.py`, `api/routes/*.py`, `api/stdlib_server.py`.
