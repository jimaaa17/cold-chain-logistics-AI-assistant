---
name: fe-product-ai-architect
description: >-
  Use this skill when designing, mocking up, scaffolding, or building features for the FE/
  JavaScript/TypeScript frontend and its FastAPI + LangGraph AI backend bridge. Guides a
  mockup-first Product Designer workflow followed by modular Full-Stack AI engineering.
---

# Product Designer & Full-Stack AI Architect Workflow

Follow this 4-stage workflow whenever creating or extending features in `FE/`:

## Stage 1: Product Discovery & UX Mockup (Designer First)
1. Map the operator's job-to-be-done (e.g., investigating a `IOT_TEMP_VAL_C > 4.0°C` excursion or auditing `FDE_VIEWS.AgentAuditLog`).
2. Produce an interactive visual mockup (using the `generative_ui` skill or a self-contained HTML prototype in `FE/design-mockups/`) showing:
   - Information hierarchy & layout grid
   - Interactive states (idle, streaming `reasoner` intent, executing `tools`, error/fallback, completed citation report)
   - Design tokens (colors, typography, spacing, status badges)
3. Validate the mockup with the user before writing production module code.

## Stage 2: Full-Stack AI Contract Design (BFF & Streaming)
1. Define the request/response and SSE event schema between `FE/src/shared/api/` and `src/api/`:
   - `POST /api/v1/dispatch/stream` -> emits `node_start`, `tool_input`, `tool_output`, `final_answer`, `error`, `done`.
   - `POST /api/v1/session/purge` -> rotates `thread_id` and clears memory checkpointer state.
   - `POST /api/v1/audit/logs` -> authenticates admin credentials server-side and returns paginated `AgentAuditLog` rows.
2. Ensure every `reasoner`, `tools`, and `reasoner_final` step writes asynchronously to `FDE_VIEWS.AgentAuditLog` on the backend.
3. Follow scalable FastAPI patterns across `src/api/server.py` and `rag/retrieval/api.py`:
   - **App Factory & Lifespan Warm-Start**: Use `create_app()` with an `@asynccontextmanager` `lifespan` to initialize pooled SQLAlchemy engines (`pool_pre_ping=True`), `httpx.Client` pools, and cached embeddings once per worker rather than per request.
   - **Temporal Tooling Coverage (Past / Present / Future)**:
     - **MSSQL Telemetry (`query_telemetry_db`)**:
       - *Past*: Historical time-window queries on `Timestamp` and forensic traces in `FDE_VIEWS.AgentAuditLog`.
       - *Present*: Real-time reefers (`Current_Temperature_C`, `Port_Congestion_Level`, `Cargo_Condition_Code`).
       - *Future*: Forward-looking risk indicators (`Delay_Probability`, `Route_Risk_Index`, `Risk_Classification`).
     - **Weather & Corridor API (`fetch_corridor_conditions`)**:
       - *Past*: Open-Meteo `past_days` parameter to correlate prior temperature excursions with ambient heatwaves/wind.
       - *Present*: `current_weather=true` for live GPS coordinates (`Latitude`, `Longitude`).
       - *Future*: `forecast_days` / `hourly=temperature_2m,windspeed_10m` for proactive route risk evaluation.
     - **SOP RAG Service (`search_compliance_sop` via FastAPI `:8001`)**:
       - *Past*: Post-mortem disposition and regulatory reporting clauses.
       - *Present*: Active excursion thresholds (`IOT_TEMP_VAL_C > 4.0°C`, `PRT_CNG_LVL > 7.0`).
       - *Future*: Preventive rerouting and Tier 1 / Tier 2 escalation protocols.

## Running the Full-Stack Product (BE + FE)
1. **SOP Retrieval Microservice (FastAPI Read Path — Port 8001)**:
   `uvicorn rag.retrieval.api:app --host 0.0.0.0 --port 8001`
2. **Dispatch BFF & Agent Gateway (FastAPI SSE Bridge — Port 8000)**:
   `uvicorn src.api.server:get_app --factory --host 0.0.0.0 --port 8000 --reload`
3. **Control Tower Frontend (`FE/` Vite + React — Port 5173, proxies `/api` -> `:8000`)**:
   `cd FE && npm run dev`

## Stage 3: Modular Frontend Implementation
1. Build pure, isolated UI components in `FE/src/shared/ui/` and domain modules in `FE/src/features/<domain>/`.
2. Keep business logic inside custom hooks/controllers (`useDispatchStream`, `useAuditLogs`, `useDispatchSession`) decoupled from view templates so they can be unit-tested independently.

## Stage 4: Verification & Reliability Checks
1. Run lint, typecheck, and unit tests (`npm test` / `vitest` and `python -m unittest`).
2. Verify offline/degraded behavior when the FastAPI backend or MSSQL/Pinecone services return sentinel warnings.
