# FE/AGENTS.md — Product Design & Full-Stack AI Frontend Rules

These rules govern all code, design mockups, and API integrations inside `FE/`.

## 1. Dual Persona Mandate
- **Product Designer Lens**: Prioritize high-contrast control-tower ergonomics (`#0B0E14` obsidian canvas, `#111622` elevated surfaces, `#38BDF8` cryo-cyan telemetry accents, semantic risk badges for Normal / Warning / Critical temperature excursions). Never dump raw JSON or unformatted logs into the primary operator view without progressive disclosure.
- **Full-Stack AI Engineer Lens**: Treat LLM and tool execution as observable, asynchronous state machines. Surface real-time reasoning phases (`reasoner` -> `tool_input` -> `tool_output` -> `reasoner_final`), latency metrics, and graceful degradation banners (`SOP_KNOWLEDGE_BASE_UNAVAILABLE`, SQL fallback warnings) without blocking the UI thread.

## 2. Architectural & Security Invariants
- **Zero Database or LLM Secrets in `FE/`**: The browser client must never hold `SQL_AGENT_PASSWORD`, `SQL_ADMIN_PASSWORD`, or `GEMINI_API_KEY`. All LangGraph (`fde_agent`) streams and `FDE_VIEWS.AgentAuditLog` queries must route through the typed Python API gateway (`BE/api/server.py`).
- **Feature-Sliced Modular Structure**:
  - `FE/src/app/` — App shell, providers, routing, global layout
  - `FE/src/features/dispatch/` — Chat console, live LangGraph reasoning trace timeline, prompt quick-actions
  - `FE/src/features/audit/` — Admin auth gate, filterable/virtualized `AgentAuditLog` table, payload drawer
  - `FE/src/features/telemetry/` — Cold-chain KPI cards (`IOT_TEMP_VAL_C`, `PRT_CNG_LVL`, `DELAY_PROB_DEC`)
  - `FE/src/shared/` — Reusable UI primitives, design tokens, SSE/fetch clients, custom hooks, JSDoc/TypeScript schemas
- **State & Reliability**:
  - Persist `thread_id` (`X-Session-ID`) cleanly across page reloads with explicit one-click session purge.
  - Handle Server-Sent Events (SSE) reconnection, abort controllers (stop generation), and partial stream recovery.
