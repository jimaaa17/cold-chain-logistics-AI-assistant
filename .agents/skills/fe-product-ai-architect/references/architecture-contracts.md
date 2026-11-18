# Full-Stack AI Contracts & Temporal Tooling Reference

## 1. BFF & Streaming Endpoints (`FE/src/shared/api/` <-> `BE/api/server.py`)
- `POST /api/v1/dispatch/stream` -> emits SSE events: `status`, `tool_input`, `tool_output`, `final_answer`, `error`, `done`.
- `POST /api/v1/session/purge` -> rotates `thread_id` and clears memory checkpointer state.
- `POST /api/v1/audit/logs` -> authenticates admin credentials server-side and returns paginated `AgentAuditLog` rows.

## 2. Scalable FastAPI Patterns
- **App Factory & Lifespan Warm-Start**: Use `create_app()` with an `@asynccontextmanager` `lifespan` to initialize pooled SQLAlchemy engines (`pool_pre_ping=True`), `httpx.Client` pools, and cached embeddings once per worker rather than per request.
- **Audit Logging**: Every `tool_input`, `tool_output`, and `final_answer` step writes to `FDE_VIEWS.AgentAuditLog` and emits observability spans.

## 3. Temporal Tooling Coverage (Past / Present / Future)
- **MSSQL Telemetry (`query_telemetry_db`)**:
  - *Past*: Historical time-window queries on `Timestamp` and forensic traces in `FDE_VIEWS.AgentAuditLog`.
  - *Present*: Real-time reefers (`Current_Temperature_C`, `Port_Congestion_Level`, `Cargo_Condition_Code`).
  - *Future*: Forward-looking risk indicators (`Delay_Probability`, `Route_Risk_Index`, `Risk_Classification`).
- **Weather & Corridor API (`fetch_corridor_conditions`)**:
  - *Past*: Open-Meteo `past_days` parameter to correlate prior temperature excursions with ambient heatwaves/wind.
  - *Present*: `current_weather=true` for live GPS coordinates (`Latitude`, `Longitude`).
  - *Future*: `forecast_days` / `hourly=temperature_2m,windspeed_10m` for proactive route risk evaluation.
- **SOP RAG Service (`search_compliance_sop`)**:
  - *Past*: Post-mortem disposition and regulatory reporting clauses.
  - *Present*: Active excursion thresholds (`IOT_TEMP_VAL_C > 4.0°C`, `PRT_CNG_LVL > 7.0`).
  - *Future*: Preventive rerouting and Tier 1 / Tier 2 escalation protocols.

## 4. Running the Full-Stack Product (BE + FE)
1. **SOP Retrieval Microservice (Port 8001)**:
   `uvicorn rag.retrieval.api:app --host 0.0.0.0 --port 8001`
2. **Dispatch BFF & Agent Gateway (Port 8000)**:
   `uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload`
3. **Control Tower Frontend (`FE/` Vite + React — Port 5173, proxies `/api` -> `:8000`)**:
   `cd FE && npm run dev`
