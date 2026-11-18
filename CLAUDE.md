# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Cold-Chain Logistics AI-Assistant: a natural-language "control tower" for logistics operators. An operator asks a question; a LangGraph agent queries fleet telemetry (legacy MSSQL), live corridor weather (Open-Meteo) and compliance SOPs (Pinecone RAG), and returns a cited answer. The target design is in `materials/Technical Design Document (TDD)_ Cold-Chain Logistics AI-Assistant.pdf`.

The repo is a **BE/ + FE/ monorepo**:

```
FE/ (React, :5173) ──/api──▶ BE/api (FastAPI BFF, :8000, SSE) ──▶ BE/agent (LangGraph dispatch workflow)
                                                                     ├─ telemetry_sql → MSSQL FDE_VIEWS.VW_ACTIVE_FLEET (USR_FDE_RO)
                                                                     ├─ corridor_api  → Open-Meteo
                                                                     └─ sop_rag       → BE/rag retrieval (in-process) → Pinecone
                                    every step → BE/observability → Langfuse + FDE_VIEWS.AgentAuditLog
```

Package-level rules live in `BE/AGENTS.md` and `FE/AGENTS.md`, and the FE visual design system is in `FE/DESIGN.md`. Read the relevant one before changing that side.

**Legacy v1 (`src/`)**: `src/ui.py` (Streamlit), `src/orchestrator.py` and `src/agent_tools.py` are the original single-process app, superseded by `BE/` + `FE/`. Don't extend it. The deploy workflow still restarts a `streamlit` systemd unit, so confirm before deleting it. `scripts/ingest_legacy_data.py` and `scripts/ingest_sop_pinecone.py` are also old standalone scripts; use `python -m legacy_ingest` / `python -m rag.ingestion` instead.

## Environment & commands

Python 3.12, local venv at `venv/`, `pip install -r requirements.txt`. Configuration comes from `.env` at the repo root. Node 20 for `FE/`.

**Import path:** the root `sitecustomize.py` puts `BE/` on `sys.path` when Python starts from the repo root, so code imports `agent`, `rag`, `api`, `observability` and `legacy_ingest` as top-level packages (never `BE.agent`). CI sets `PYTHONPATH=BE:.` for the same effect, and `pyproject.toml` sets the pytest `pythonpath`.

```bash
# Backend tests (unittest, fully offline: fakes for LLM, SQL, embedder and Pinecone)
python -m unittest discover -s tests -v
python -m unittest tests.test_rag_services.TestIngestion                                            # one class
python -m unittest tests.test_rag_services.TestIngestion.test_shrunk_document_prunes_stale_chunks   # one test

# Agent from the CLI
python -m agent --dry-run                       # validate config and guards only
python -m agent --prompt "..." [--llm GEMINI|OPENAI|OLLAMA] [--session-id X] [--decision approve|reject]
python -m agent --legacy-react --prompt "..."    # previous single ReAct loop, for comparison

# API gateway / BFF
uvicorn api.server:app --host 127.0.0.1 --port 8000   # FastAPI app
python -m api.server                                  # zero-dependency stdlib fallback server (PORT env, default 8000); also serves FE/dist

# Frontend (Vite proxies /api → 127.0.0.1:8000)
cd FE && npm install && npm run dev             # :5173
cd FE && npm test && npm run build              # vitest + production build (what CI runs)

# SOP ingestion (write path) → Pinecone
python -m rag.ingestion --dry-run               # plan only; loads no model, touches no index
python -m rag.ingestion                         # incremental sync of SOP_SOURCE_DIR (default data/policy/)
python -m rag.ingestion --full-rebuild [--allow-index-recreate]

# Standalone retrieval service (optional; the agent calls retrieval in-process by default)
uvicorn rag.retrieval.api:app --host 0.0.0.0 --port 8001   # /healthz, /readyz, POST /v1/retrieve

# Legacy telemetry load: CSV (Kaggle URL in data/source/data.txt) → MSSQL dbo.TBL_SC_FLEET_HIST_RAW
python -m legacy_ingest [--dry-run] [--mode refresh|upsert] [--csv PATH] [--force]   # needs ODBC Driver 18
```

`docs/instructions.md` covers starting the MSSQL container (`mcr.microsoft.com/mssql/server:2022-latest` in Docker, locally or on EC2), checking the load (expected 32,065 rows) and the systemd setup. After loading, run `BE/sql/setup_security_view.sql` as admin to create the view, the RO login and the audit table.

**CI/CD** (`.github/workflows/deploy.yml`): on push/PR to `main` it runs the backend unittest suite, then `npm install && npm test && npm run build` in `FE/`. Deployment to EC2 over SSH runs only on manual `workflow_dispatch`: it does a git pull, installs deps, builds FE, and restarts the `fde-api` and `streamlit` systemd units.

## Data & security model

- **Legacy schema.** `legacy_ingest` deliberately renames the clean dataset columns to "messy legacy" names: `TS_UTC, V_LAT, V_LON, IOT_TEMP_VAL_C, CGO_COND_CD, RISK_CLS_TXT, DELAY_PROB_DEC, PRT_CNG_LVL, RT_RSK_IDX, SYS_INGEST_FLAG`. The SOP (`data/policy/Cold_Chain_Incident_SOP_v2.md`) refers to these exact names in its thresholds (`IOT_TEMP_VAL_C > 4.0`, `PRT_CNG_LVL > 7.0`, High Risk with `DELAY_PROB_DEC > 0.65`), which ties the telemetry data to the SOP knowledge. Keep them consistent.
- **Semantic view.** `BE/sql/setup_security_view.sql` creates `FDE_VIEWS.VW_ACTIVE_FLEET`, which exposes the legacy columns under readable aliases (`Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code, Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index`). The agent and its prompt use the **aliases**; the raw table and the SOP use the **legacy names**.
- **Least privilege.** `USR_FDE_RO` may only `SELECT` from the view and `INSERT` into `FDE_VIEWS.AgentAuditLog`. Reading the audit log requires `SQL_ADMIN_USER` credentials, which the FE audit screen collects and the BFF forwards per request. `telemetry_sql.validate_readonly_select` is a second line of defence: it allows a single `SELECT` against the view only, rejects DDL/DML/`EXEC`/`xp_`/`sp_`, comments and `;`, and caps rows (`SQL_MAX_ROWS`, default 10).
- **Audit table** columns as actually created: `LogID, Timestamp, SessionID, NodeExecuted, ToolName, Content`. This differs from the TDD's `{User_Prompt, Tool_Invoked, Tool_Raw_Output, LLM_Decision}`; the code writes to the real columns.
- **No secrets in `FE/`.** The browser never holds SQL or LLM keys; everything goes through `BE/api`.

## Backend packages (`BE/`)

### `agent/`: agentic dispatch workflow
- `workflow.py`: the LangGraph state machine the BFF and CLI run. `intake` routes the question in code (`intent.py`): `incident_scan` fans out telemetry + weather + SOP in parallel with `Send` (no LLM planning call), `sop_lookup` searches the SOP only, `open` lets an LLM `planner` pick tool calls, `clarify` answers without tools. Results go to `ledger` (`evidence.py`: records with source, fields, `as_of`, and `fresh`/`historical`/`undated`/`forecast` by `EVIDENCE_FRESHNESS_HOURS`, default 24) and `rules` (`rules.py`: SOP §1–§3 as code; only fresh telemetry triggers actions; weather never does). The `reasoner` LLM writes from the ledger and tags actions as `ACTION[id] on [T1]`; the `verifier` (`verifier.py`, plain code) checks numbers, citations, permitted actions, freshness wording and trigger coverage, sends the draft back **once**, then labels it unverified. `action_gate` calls `interrupt()` for high-stakes actions (diversions, write-offs) and resumes with the dispatcher's decision; `finalize` renders actions, verification and the decision. Nodes emit events through the stream writer; `iter_dispatch_events` adapts them (plus reasoner tokens) for the BFF. `DispatchResources` holds the shared LLM, SQL engine, HTTP client and `RetrievalService`, and `warm()` pre-loads them.
- `prompts/reasoner_prompt.txt`: the reasoner's rules and answer layout. The verifier enforces rules 1–5, so keep the two consistent.
- When the SOP changes, update `rules.py` (thresholds, permitted actions, `HIGH_STAKES_ACTIONS`). The emergency cold-storage diversion is not permitted because telemetry has no ETA-delay field.

The previous single ReAct loop remains for comparison (`--legacy-react`) and its tests:
- `graph.py`: builds the `StateGraph` (reasoner ⇄ `ToolNode`, `tools_condition`, `MemorySaver` checkpoint keyed by `thread_id` = session id). It has no import-time side effects; the LLM, engine and vector store are built lazily or injected (tests inject fakes). `build_reasoner_llm()` selects `AGENT_LLM` = `GEMINI` (default model `gemini-3.8-flash`), `OPENAI` (`gpt-4o`) or `OLLAMA` (`qwen2.5:7b`). `format_message_content()` normalizes Gemini's multi-block content.
- `config.py`: pydantic-settings `AgentSettings` / `get_agent_settings()`, covering the LLM, SQL connection, corridor API, `RAG_TOP_K`, audit and Langfuse settings, and `SYSTEM_PROMPT_PATH`.
- `tools/`: factory functions (`make_telemetry_sql_tool`, `make_corridor_conditions_tool`, `make_inprocess_sop_tool`) that return LangChain tools. Every tool **degrades instead of raising**: it returns an explanatory string (validation error, API unavailable, or the SOP sentinels `SOP_KNOWLEDGE_BASE_UNAVAILABLE` / `SOP_QUERY_REJECTED` / `NO_MATCHING_SOP`), so the LLM reports missing or stale data rather than inventing it. Transient failures retry via `rag.core.resilience.with_retry`.
- `prompts/system_prompt.txt`: the persona, tool order (telemetry → corridor → SOP) and a strict Markdown answer template (a single shipment table, 🔴 for > 4.0 °C, SOP citation). The FE renders this Markdown, so prompt format changes are UI changes.
- `audit.py` (`AuditLogger`) and `tracing.py` (trace/span IDs).

### `api/server.py`: Backend-for-Frontend gateway
- `create_app(agent_streamer=, audit_writer=, audit_reader=)` lets you inject fakes (see `tests/test_fe_api.py`). The module-level `app` is for uvicorn.
- Endpoints: `GET /healthz`, `GET /api/v1/meta` (active LLM and model, embeddings, observability status, `config_errors`), `GET /api/v1/traces`, `POST /api/v1/session/purge` (deletes the old thread's checkpoint, returns a new id), `POST /api/v1/dispatch/stream` (SSE), `POST /api/v1/audit/logs` (admin creds in body). `/meta` and purge are built by shared functions (`build_meta_payload`, `build_purge_payload`) so both servers return the same thing.
- Audit: each dispatch run writes the operator prompt (`user_input` / `Dispatcher`) and then one row per step through `SqlAuditLedgerExporter`, using the telemetry engine from `AgentSettings` (configured database, `AUDIT_ENABLED`, `AUDIT_TABLE`). The audit reader uses the same server and database with the admin credentials from the request.
- SSE event types: `status`, `tool_input`, `tool_output`, `evidence`, `draft_start`, `token`, `verification`, `approval_required`, `approval_recorded`, `final_answer`, `error`, `done` (with `outcome`: `complete` | `awaiting_approval` | `error`). Tokens, `draft_start` and `status` are streamed only; every other step is audited. The FE applies them in `features/dispatch/hooks/dispatchEvents.js`, so change both sides together.
- `POST /api/v1/dispatch/approve` (`{session_id, decision: approve|reject, by, note}`) resumes a run paused at the action gate and streams the rest as SSE; the decision is audited first (`approval` / `Dispatcher`). There is no authentication: `by` is whatever the client sends.
- Startup pre-warms the workflow (`prewarm_agent`: graph, LLM client, SQL engine, embedding model with one probe embedding) unless `AGENT_PREWARM=false`; failures are logged, not fatal. Gemini runs with `GEMINI_THINKING_BUDGET=0` by default (measured first token 13.4 s → 6.4 s); unset it for the model default.
- If the graph can't be built (bad settings, missing key or provider package), `_get_compiled_graph` raises `AgentConfigError` and the stream ends with an SSE `error` frame naming the problem (field/reason only, never raw values). Runtime errors during the run surface the same way. The canned `_offline_fallback_streamer` stream is opt-in via `AGENT_OFFLINE_DEMO=1` for UI work without keys (ignored when `APP_ENV=production`), and is used only when the graph can't be built: never spliced into, or substituted for, a live run (mid-run failures and empty runs end with an `error` frame). Its events carry `simulated: true`, which adds a `status` frame with `phase: "simulated"`, a `simulated` flag on every frame, a `[SIMULATED: AGENT_OFFLINE_DEMO]` prefix on audit/span content, and an amber banner in `MessageBubble`. There is no fallback to legacy `src.orchestrator`.
- `run_stdlib_server()` mirrors the same endpoints with only the stdlib (`python -m api.server`) and must be kept in sync with the FastAPI routes.

### `observability/`: tracing and compliance audit
`ObservabilityPipeline` fans spans (`SpanKind`, `TraceContext`, `models.py`) out to pluggable exporters in `exporters/`: `LangfuseSpanExporter`, `SqlAuditLedgerExporter` (writes `AgentAuditLog`) and `InMemorySpanExporter` (ring buffer, used by `/api/v1/traces` and tests). It is configured from env via `ObservabilitySettings.from_env()` (`LANGFUSE_*`, `AUDIT_ENABLED`, `AUDIT_TABLE`, `OBSERVABILITY_BUFFER_SIZE`). Every agent turn must reach both sinks.

### `rag/`: SOP RAG, split along the read/write boundary
- `config.py`: a single pydantic-settings `Settings` (env vars matched case-insensitively) via the cached `get_settings()`. `EMBEDDINGS_PROVIDER` is `LOCAL` (default `BAAI/bge-m3`, 1024-dim) or `OPENAI`. The index name defaults to `fde-sop-index-<provider>`.
- `core/`: `Embedder` (batching, retries, dimension check, a lock that serialises local torch models), `VectorStore` (one Pinecone index and namespace), `with_retry` (retries only transient 429/5xx/timeouts), and dataclass models.
- `ingestion/`: an offline batch job, run with concurrency 1.
  - **Manifest** (`data/cache/manifest__<index>__<namespace>.json`) records each file's sha256 and *pipeline fingerprint* (provider, model, dims, chunking, document_type, `PIPELINE_VERSION`). **Bump `PIPELINE_VERSION` in `rag/config.py` (currently `"2"`) whenever parsing, chunking or metadata output changes**, so every document gets re-embedded.
  - Vector IDs are deterministic (`<doc_key>#<00000>`), which allows deletion by prefix listing (serverless indexes have no delete-by-filter). A document is upserted first and its stale IDs pruned afterwards. A per-document failure never aborts the run and that document isn't recorded, so the next run retries it. Exit codes: 0 ok, 1 some docs failed, 2 config error.
  - Parsers are registered by extension with `@register(".ext")` in `parsers.py`: md/markdown, txt, pdf, csv/xlsx, and images (png/jpg/jpeg/webp/bmp/tiff), with image handling in `multimodal.py`. Parsers raise `DocumentParseError` rather than return `[]`. Markdown chunks get their header breadcrumb prepended; PDF chunks keep page numbers for citations.
  - Only ingestion may create an index (`provision=True`). A dimension mismatch drops the index only when `ALLOW_INDEX_RECREATE` is set.
- `retrieval/`: `service.py` holds the transport-independent `RetrievalService` with an LRU query-embedding cache, and the agent's `sop_rag` tool calls it **in-process**. `api.py` is an optional stateless FastAPI wrapper (`create_app(service_factory)`), and `client.py` is the light HTTP `RetrievalClient` that defines the sentinel strings.

### `legacy_ingest/`: standalone telemetry CSV loader
An extract → transform → validate → load pipeline (`extract.py`, `transform.py`, `validate.py`, `loader.py`, `db.py`, `schema.py`, `audit.py`) with a reject-ratio guard and a skip-if-unchanged check. It **must not import from `rag` or `agent`.**

### Cross-cutting rule
Heavy imports (`torch`, `pinecone`, `langchain_*` backends, `sqlalchemy` engines) are deferred inside functions or factories so that `--help`/`--dry-run` start instantly and the tests run offline. Keep that pattern in every package.

## Frontend (`FE/`)

React 18 + Vite 6 + Tailwind 3, tested with vitest. The code is plain JS/JSX, organised feature-sliced:
- `src/app/`: the shell (`App.jsx`, `Sidebar.jsx`).
- `src/features/dispatch/`: the chat console, `useDispatchStream` (the SSE consumer with abort/stop), `ReasoningTimeline`, `ToolTraceAccordion` and `MessageBubble`.
- `src/features/audit/`: the admin-gated `AuditLogView` with `useAuditLogs`.
- `src/features/telemetry/`: KPI cards (`TelemetryKpiBar`).
- `src/shared/`: `api/endpoints.js` + `api/sseClient.js` (base URL from `VITE_API_BASE_URL`, default same-origin), `config/designTokens.js` (dark control-tower palette), and `ui/Primitives.jsx`.

The session id (`thread_id`) persists across reloads and can be purged with one click. `FE/design-mockups/*.html` are static design references, and `.agents/skills/fe-product-ai-architect/` is a project skill for FE design work.

## Tests (`tests/`)

- `test_rag_services.py`: ingestion and retrieval.
- `test_agent_and_multimodal_rag.py`: graph, tools and image SOPs.
- `test_fe_api.py`: BFF contract and SSE frames.
- `test_dispatch_workflow.py`: intent routing, ledger, rules, verifier, the workflow graph (fan-out, retry, unverified, approval) and the approve endpoint, with fake LLM, SQL, weather and retrieval.
- `test_sop_rules.py`: SOP rule regressions for F7 (weather never triggers; congestion only from telemetry in the LB/LA area).
- `test_legacy_ingest.py`: the loader.
- `review_findings_test.py` and `open_findings/`: regression tests for code-review findings (reviews in `materials/code-review/`).

All tests must stay offline.
