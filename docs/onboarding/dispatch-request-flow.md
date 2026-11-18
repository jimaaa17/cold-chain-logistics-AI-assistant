# Onboarding: the Dispatch request path, end to end

This document covers one area in depth: what happens when an operator types a question into the
Control Tower and presses Enter. If you understand this path, you understand how every package in the
system connects (`FE/`, `BE/api`, `BE/agent`, `BE/rag`, `BE/observability`, MSSQL, Pinecone, Langfuse).

Read it with the code open. Every hop below names the file and function to look at.

> **Warning: "the UI works" does not mean "the system works".** Until PR #8, the BFF replaced a failed
> agent run with a canned, realistic-looking answer (finding F1 below). That is fixed: failures now end
> in an `error` frame, and the opt-in demo (`AGENT_OFFLINE_DEMO=1`, never in production) is labelled
> *simulated*. Still check every leg separately before you trust an end-to-end demo.

---

## 1. The picture

![Dispatch Request Flow (Tokyo Night SVG)](./assets/dispatch-request-flow.svg)

<details>
<summary><strong>Terminal / ASCII Art View (rendered via <code>pretty-mermaid</code>)</strong></summary>

```text
┌────────────────────┐                                 ┌────────────────────┐                                   ┌─────────────────┐                                                  ┌───────────────────────────────┐  ┌──────────────────────────┐
│ Browser (FE :5173) │                                 │ BFF (BE/api :8000) │                                   │ LangGraph Agent │                                                  │ MSSQL / Open-Meteo / Pinecone │  │ AgentAuditLog / Langfuse │
└──────────┬─────────┘                                 └──────────┬─────────┘                                   └────────┬────────┘                                                  └───────────────┬───────────────┘  └─────────────┬────────────┘
           │                                                      │                                                      │                                                                           │                                │
           │  POST /api/v1/dispatch/stream {session_id, prompt}   │                                                      │                                                                           │                                │
           │──────────────────────────────────────────────────────▶                                                      │                                                                           │                                │
           │                                                      │                                                      │                                                                           │                                │
           │         event: status (1st frame, trace_id)          │                                                      │                                                                           │                                │
           ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│                                                      │                                                                           │                                │
           │                                                      │                                                      │                                                                           │                                │
           │                                                      │  default_agent_streamer -> graph.stream(thread_id)   │                                                                           │                                │
           │                                                      │──────────────────────────────────────────────────────▶                                                                           │                                │
           │                                                      │                                                      │                                                                           │                                │
       ┌loop [ReAct Loop (reasoner <-> ToolNode)]─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                                                      │              reasoner (LLM) tool_calls               │                                                                           │                                │   │
       │   │                                                      ◀──────────────────────────────────────────────────────│                                                                           │                                │   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                                                      │                                                      │             INSERT AgentAuditLog + span                                   │                                │   │
       │   │                                                      │───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────▶   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                  event: tool_input                   │                                                      │                                                                           │                                │   │
       │   ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│                                                      │                                                                           │                                │   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                                                      │                                                      │  query_telemetry_db | fetch_corridor_conditions | search_compliance_sop   │                                │   │
       │   │                                                      │                                                      │───────────────────────────────────────────────────────────────────────────▶                                │   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                                                      │                                                      │                        Rows / Weather / SOP chunks                        │                                │   │
       │   │                                                      │                                                      ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│                                │   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                                                      │                     ToolMessage                      │                                                                           │                                │   │
       │   │                                                      ◀──────────────────────────────────────────────────────│                                                                           │                                │   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                                                      │                                                      │             INSERT AgentAuditLog + span                                   │                                │   │
       │   │                                                      │───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────▶   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       │   │                 event: tool_output                   │                                                      │                                                                           │                                │   │
       │   ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│                                                      │                                                                           │                                │   │
       │   │                                                      │                                                      │                                                                           │                                │   │
       └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
           │                                                      │                                                      │                                                                           │                                │
           │                                                      │      reasoner (LLM) final text (no tool_calls)       │                                                                           │                                │
           │                                                      ◀──────────────────────────────────────────────────────│                                                                           │                                │
           │                                                      │                                                      │                                                                           │                                │
           │                                                      │                                                      │             INSERT AgentAuditLog + span                                   │                                │
           │                                                      │───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────▶
           │                                                      │                                                      │                                                                           │                                │
           │        event: final_answer (or event: error)         │                                                      │                                                                           │                                │
           ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│                                                      │                                                                           │                                │
           │                                                      │                                                      │                                                                           │                                │
           │          event: done (always, in finally)            │                                                      │                                                                           │                                │
           ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│                                                      │                                                                           │                                │
           │                                                      │                                                      │                                                                           │                                │
┌──────────┴─────────┐                                 ┌──────────┴─────────┐                                   ┌────────┴────────┐                                                  ┌───────────────┴───────────────┐  ┌─────────────┴────────────┐
│ Browser (FE :5173) │                                 │ BFF (BE/api :8000) │                                   │ LangGraph Agent │                                                  │ MSSQL / Open-Meteo / Pinecone │  │ AgentAuditLog / Langfuse │
└────────────────────┘                                 └────────────────────┘                                   └─────────────────┘                                                  └───────────────────────────────┘  └──────────────────────────┘
```

</details>

Side effects on every step:
- **`FDE_VIEWS.AgentAuditLog`**: one INSERT per `tool_input`, `tool_output` and `final_answer`, written as `USR_FDE_RO`.
- **Observability pipeline**: one span per step in the in-memory ring buffer (`/api/v1/traces`) and in Langfuse when keys are set.

---

## 2. Hop by hop

| # | Hop | Where | What to notice |
|---|-----|-------|----------------|
| 1 | UI submit | [useDispatchStream.js](../../FE/src/features/dispatch/hooks/useDispatchStream.js) | Creates a user message and a placeholder assistant message with `state: "running"`. Every SSE event updates *that* placeholder by id. The AbortController powers the Stop button. |
| 2 | HTTP + SSE parse | [endpoints.js](../../FE/src/shared/api/endpoints.js), [sseClient.js](../../FE/src/shared/api/sseClient.js) | This is `fetch` POST, not `EventSource`, because EventSource can't POST. Frames are split on `\n\n`, and `data:` is parsed as JSON (on failure it falls back to `{raw}`). The base URL is `VITE_API_BASE_URL` or same-origin; in dev, the Vite proxy forwards `/api` to `127.0.0.1:8000`. |
| 3 | BFF route | [server.py](../../BE/api/server.py) `create_app → stream_dispatch` | pydantic requires non-empty `session_id` and `prompt`. The streamer, audit writer and audit reader are **injectable**, which is how `tests/test_fe_api.py` runs offline. |
| 4 | Frame engine | `server.py` `generate_dispatch_sse_frames` | This pure generator is the SSE contract. It turns each step into an audit write, a span and a frame. If no `final_answer` arrives it emits `error`, and it always emits `done`. The event names are a **shared contract with the FE**, so change both sides together. |
| 5 | Graph adapter | `server.py` `default_agent_streamer`, `_get_compiled_graph` | Builds the graph once (module-global cache), runs `graph.stream(stream_mode="updates")`, and maps reasoner `tool_calls` → `tool_input`, `ToolMessage` → `tool_output`, and reasoner text without tool calls → `final_answer`. Build errors become a redacted `AgentConfigError`; run errors propagate to an `error` frame. This is where failures used to be hidden (F1, F2, fixed in #8). |
| 6 | Graph | [graph.py](../../BE/agent/graph.py) `build_agent_graph` | A ReAct loop: `START → reasoner ⇄ tools`, routed by `tools_condition`. `MemorySaver` keys conversation memory by `thread_id = session_id`. The system prompt is re-read from disk and placed at position 0 on *every* reasoner call. The LLM comes from `build_reasoner_llm(AgentSettings)`. |
| 7a | Telemetry tool | [telemetry_sql.py](../../BE/agent/tools/telemetry_sql.py) | `validate_readonly_select` allows one `SELECT` against `FDE_VIEWS.VW_ACTIVE_FLEET` only, with no comments or `;`. The engine is lazy and the query runs as `USR_FDE_RO`, capped at `SQL_MAX_ROWS`. Errors come back as **strings**, not exceptions. |
| 7b | Corridor tool | [corridor_api.py](../../BE/agent/tools/corridor_api.py) `fetch_corridor_conditions` | Calls Open-Meteo through httpx with `with_retry` (transient errors only), after validating coordinate and day bounds. |
| 7c | SOP tool | [sop_rag.py](../../BE/agent/tools/sop_rag.py) `search_compliance_sop` | Calls `RetrievalService.from_settings(rag.config.get_settings())` in-process, lazily: the embedder (bge-m3, torch) and the Pinecone client load on the **first call**. It degrades to `SOP_KNOWLEDGE_BASE_UNAVAILABLE`, `SOP_QUERY_REJECTED` or `NO_MATCHING_SOP`. |
| 8 | Audit / trace | `server.py` `default_audit_writer`; [pipeline.py](../../BE/observability/pipeline.py) | The prompt and every step are written through `SqlAuditLedgerExporter`, using the telemetry engine built from `AgentSettings` (INSERT-only role). A failed write is logged and never breaks the stream. Spans go to the ring buffer and Langfuse. |
| 9 | Render | `FE/src/features/dispatch/components/` | `ReasoningTimeline` and `ToolTraceAccordion` render `traces[]`, and `MessageBubble` renders the Markdown answer. The answer layout is defined in [system_prompt.txt](../../BE/agent/prompts/system_prompt.txt), so **prompt edits are UI edits**. |

### Data contracts that this path depends on
- **View aliases vs legacy names.** The agent queries the view's aliases (`Current_Temperature_C`, …). The SOP states thresholds in legacy names (`IOT_TEMP_VAL_C > 4.0`). The mapping lives in [setup_security_view.sql](../../BE/sql/setup_security_view.sql).
- **Least privilege.** `USR_FDE_RO` = `SELECT` on the view plus `INSERT` on the audit table. Reading the audit log needs admin credentials, which are collected per request on the Audit screen.
- **Config.** [agent/config.py](../../BE/agent/config.py) (`AgentSettings`) and [rag/config.py](../../BE/rag/config.py) (`Settings`, which *requires* `PINECONE_API_KEY`), both loaded from the repo-root `.env`.

---

## 3. Questions to ask (and where to find the answers)

Work through these in order. Each one either has a short answer in the code or marks a real gap.

**Architecture and contracts**
1. What is the SSE contract (event names and payload fields)? Where is it tested on the BE side (`tests/test_fe_api.py`) and on the FE side (vitest)?
2. Why are there two BFFs (FastAPI `create_app` and the stdlib `run_stdlib_server`)? Which one does the `fde-api` systemd unit run? What keeps them in sync?
3. Why does the BFF call `graph.stream` directly instead of `AgentOrchestrator.stream` ([graph.py](../../BE/agent/graph.py)), which also audits the user prompt and attaches the Langfuse LangChain callback?

**Session and state**
4. `thread_id = session_id` in a process-local `MemorySaver`. What happens to memory on a restart, with more than one uvicorn worker, or after `/session/purge`? (Purge only mints a new UUID.)
5. How large does a long thread get? Is there any message trimming?

**Agent and LLM**
6. Which LLM actually runs? `AgentSettings` resolves `AGENT_LLM | Agent_llm | llm`, while `/api/v1/meta` re-derives it with `os.getenv`. Can they disagree?
7. What bounds the ReAct loop (`recursion_limit`)? Is there a per-request timeout?
8. How does the LLM map legacy SOP threshold names to the view aliases it has to query? Does the prompt say so?

**Tools and data**
9. Is the Pinecone index (`fde-sop-index-<provider>`) populated? Where is `PINECONE_API_KEY` set? Does `data/cache/manifest__*.json` exist?
10. Is MSSQL reachable from your machine? Which database holds the view? (The BFF audit engine hard-codes `master`.)
11. What does the first SOP call cost (bge-m3 cold load inside a live request)?

**Observability and audit**
12. Does every turn reach both sinks? Is the *user prompt* audited on the BFF path? Where do audit failures go?
13. How do you follow one request from FE → AgentAuditLog → Langfuse? (`trace_id` appears in SSE and spans, but the audit table has no trace_id column.)

**Failure semantics**
14. When something fails, what does the operator see, and can they tell real data from simulated data?

---

## 4. Known findings (as of 2026-11-18)

The offline suite passes (101 tests, 1 skipped, after #8) and `python -m agent --dry-run` is OK, yet the live path can still be broken. All six findings are resolved:

| ID | Severity | Finding | Location | Status |
|----|----------|---------|----------|--------|
| F1 | Critical | `default_agent_streamer` catches **every** exception, or a run that emits nothing, and yields `_offline_fallback_streamer`: hard-coded "BREACH 6.8 °C" rows and an SOP citation. A mid-run failure gives real events **followed by fabricated ones**, all of which are audited as genuine. The UI has no "simulated" marker. | `BE/api/server.py` `default_agent_streamer` | **Fixed in #8**: no canned data after a failure; demo is opt-in, refused when `APP_ENV=production`, and marked `simulated`. |
| F2 | High | `_get_compiled_graph` falls back to the legacy `src.orchestrator.fde_agent` on any build error, which hides config errors. | `BE/api/server.py` `_get_compiled_graph` | **Fixed in #8**: no legacy fallback; redacted `AgentConfigError`, retried on the next request. |
| F3 | High | The SOP leg is likely dead locally: there is no `PINECONE_API_KEY` in `.env` and no ingestion manifest, so the tool returns `SOP_KNOWLEDGE_BASE_UNAVAILABLE`. | `.env`, `data/cache/` | **Resolved 2026-11-18** (no code change): the key was present, but the old script had loaded the SOP into the default namespace. `python -m rag.ingestion` loaded it into `sop`. |
| F4 | Medium | The BFF audit path duplicates config (raw `os.getenv`, `DATABASE=master`, empty-password default), never audits the user prompt, and `print`s its failures. | `BE/api/server.py` `default_audit_writer`, `_build_mssql_engine` | **Fixed in #12**: writer reuses `SqlAuditLedgerExporter` with the engine from `AgentSettings` (configured database, `AUDIT_ENABLED`, `AUDIT_TABLE`); the reader uses the same server and database with the admin credentials; the prompt is audited as `user_input` / `Dispatcher`; failures are logged. |
| F5 | Medium | Drift: `/meta` computes the LLM differently from `AgentSettings`; the stdlib `/meta` has no `capabilities`; the canned fallback SQL uses legacy column names against the view. | `BE/api/server.py` | **Fixed in #12**: one `build_meta_payload()` from `AgentSettings` and the `rag` settings, shared by both servers (a test compares them); the demo SQL uses the view aliases. |
| F6 | Low | `/session/purge` doesn't clear the checkpointer. | `BE/api/server.py` `purge_session` | **Fixed in #12**: purge calls `checkpointer.delete_thread(old_session_id)`; `purged` reports the outcome. |

---

## 5. Triage: the E2E path is broken. Where do I look?

| Symptom | Likely hop | First check |
|---------|-----------|-------------|
| Nothing streams; the browser shows an HTTP error | 2–3 | Is `uvicorn api.server:app` up? `curl localhost:8000/healthz`. Is the Vite proxy target right? |
| An amber "OFFLINE DEMO" banner, or the same "BREACH 6.8 °C @ 33.74,-118.27" answer every time | 5 | The graph couldn't be built and `AGENT_OFFLINE_DEMO=1` is set. Unset it to see the real error, or run `python -m agent --prompt "..."`. |
| `tool_output` says `Database Error: …` | 7a | ODBC Driver 18 installed? Host and port reachable? `USR_FDE_RO` created by `setup_security_view.sql`? |
| `tool_output` says `SECURITY BLOCK: …` | 7a | The LLM wrote invalid SQL (legacy names, comments, a second statement). Check the prompt guidance. |
| `SOP_KNOWLEDGE_BASE_UNAVAILABLE` | 7c | `PINECONE_API_KEY`, the index name, whether ingestion has run (`python -m rag.ingestion --dry-run`). |
| `NO_MATCHING_SOP` | 7c | The index is empty or in the wrong namespace or provider (LOCAL vs OPENAI use different indexes). Vectors written by the old `scripts/ingest_sop_pinecone.py` sit in the *default* namespace, not `sop`. |
| An `error` frame saying "Execution Timeout…" | 4–6 | The LLM never produced a final text. Check the provider key and model name (`GEMINI_MODEL`). |
| The answer renders but the audit screen is empty | 8 | Look for "SQL Audit ledger write skipped" or "Audit ledger unavailable" warnings in the server log. The audit reader quietly falls back to in-memory spans when MSSQL is unreachable. |

Leg-by-leg commands:
```bash
python -m agent --dry-run                                  # config + guards
python -m agent --prompt "Top 3 hottest shipments?"        # graph without the BFF (real errors surface here)
python -m rag.ingestion --dry-run                          # SOP index plan
curl -N -X POST localhost:8000/api/v1/dispatch/stream \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"debug-1","prompt":"Shipments above 4C?"}' # raw SSE frames
curl 'localhost:8000/api/v1/traces?session_id=debug-1'      # spans for that run
```

---

## 6. Short-term repair plan (one PR per step, in order)

0. **Make breakage visible.** Add `python -m agent --smoke`, which checks each leg (LLM, view `SELECT TOP 1`, Open-Meteo, SOP retrieve, audit insert) and prints PASS/FAIL with the reason. Add a `/readyz` endpoint that reports the same.
1. ✅ **Never fabricate (F1, F2), done in #8.** Put the canned stream behind `AGENT_OFFLINE_DEMO=1` and mark its frames `simulated: true`, with a UI badge. Otherwise an exception becomes an `error` frame with the real reason. Never fall back after real events have streamed. Remove the `src.orchestrator` fallback. Add tests in `tests/test_fe_api.py`.
2. **Restore SOP RAG (F3).** ✅ Ingestion run and retrieval verified (2026-11-18). Still to do: the smoke check. Add the legacy-name → alias mapping to the system prompt.
3. ✅ **Unify audit and config (F4, F5), done in #12.** Build the BFF audit and `/meta` from `AgentSettings` and `rag` settings, audit the user prompt, and log failures through `logging`.
4. **Session and guardrails (F6).** ✅ Purge clears the checkpoint (#12). Still to do: `recursion_limit` and a request timeout.

Deferred: broader stdlib/FastAPI parity tests (`/meta` and purge are covered), a trace_id column in AgentAuditLog, a shared checkpointer for multiple workers.

**Done means:** with a real `.env`, the question *"Which shipments are above 4 °C near Long Beach and what does the SOP require?"* produces three real tool traces, an SOP citation from `Cold_Chain_Incident_SOP_v2.md`, no simulated badge, matching rows (including the prompt) on the Audit screen, and spans at `/api/v1/traces`. Breaking `GEMINI_API_KEY` must produce a clear error, not a report.
