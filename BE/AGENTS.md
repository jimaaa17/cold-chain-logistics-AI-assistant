# BE/AGENTS.md — Backend AI, Data Engineering & BFF Gateway Rules

These rules govern all Python backend packages inside `BE/`.

## 1. Modular Package Boundaries
- `BE/api/` — FastAPI & SSE Backend-for-Frontend (`server.py`). Bridges `FE/` to `BE/agent/` and `BE/observability/`.
- `BE/observability/` — Cross-cutting AI Observability & Compliance subsystem (`ObservabilityPipeline`, `SpanKind`, `TraceContext`, and pluggable `exporters/` for `LangfuseSpanExporter`, `SqlAuditLedgerExporter`, and `InMemorySpanExporter`).
- `BE/agent/` — agentic dispatch workflow (`dispatch/`, re-exported by `workflow.py`), legacy ReAct graph (`graph.py`), config (`config.py`), and tools (`telemetry_sql.py`, `corridor_api.py`, `sop_rag.py`).
  - `BE/agent/dispatch/` — `graph.py` holds topology only (`add_node` / `add_edge`); each step is a module-level function in `nodes/` taking `(state, runtime: Runtime[DispatchContext])`. **Dispatch nodes get the LLM, SQL engine, HTTP client and retrieval service from `runtime.context.resources`, settings from `runtime.context.settings` and the time from `runtime.context.clock()`; they never build or import clients themselves.** They emit events through `runtime.stream_writer`. Run the graph through `streaming.iter_dispatch_events`, which passes `context=graph.dispatch_context`.
- `BE/rag/` — Pinecone SOP ingestion (`BE/rag/ingestion/`) and retrieval (`BE/rag/retrieval/`).
- `BE/legacy_ingest/` — Standalone MSSQL telemetry CSV loader. Must never import from `rag` or `agent`.
- `BE/sql/` — Database security views and RBAC DDL (`setup_security_view.sql`).

## 2. Security & Reliability Invariants
- **Read-Only Telemetry Queries**: `telemetry_sql.py` must only query `FDE_VIEWS.VW_ACTIVE_FLEET` using `SELECT` statements with the `USR_FDE_RO` role.
- **Dual-Sink Audit & Traceability**: Every agent turn must record spans in `LangfuseBridge` and append to `FDE_VIEWS.AgentAuditLog` (`GRANT INSERT TO USR_FDE_RO`). Reading `AgentAuditLog` requires `SQL_ADMIN_USER`.
- **Deferred Heavy Imports**: Keep heavy third-party imports (`torch`, `pinecone`, `langchain_*`, `sqlalchemy`) inside functions/factories so `--dry-run`, `--help`, and offline unit tests execute instantaneously.
