# AI Engineer Job Aid: From Junior to Senior IC on the Cold-Chain Dispatch Stack

> **Note (2026-11-18):** the exercises that catch findings F1–F3 (the canned fallback, the legacy
> `src.orchestrator` fallback and the empty SOP namespace) describe the code **before** the fix in PR #8.
> To reproduce them, check out the pre-fix code: `git switch --detach 1fe6ca4`. Line numbers below refer
> to that commit. On current `main`, the expected result is an SSE `error` frame instead of a fabricated report.

This job aid is a hands-on, step-by-step runbook. Instead of passively reading files, you will run **four progressive verification phases** across the `POST /api/v1/dispatch/stream` path. 

At each step, you will:
1. **Inspect** the exact module and line numbers.
2. **Execute** a targeted CLI or script check.
3. **Verify** the expected output vs. the **Senior IC Catch** (the architectural gap or hidden failure mode that separates a Junior who says *"tests pass!"* from a Senior who says *"the system is silently fabricating data"*).

---

## Pre-Flight: Environment & Baseline Check

Before starting Phase 1, activate the repository's virtual environment (`venv/`, created with `uv`) and confirm your baseline from the repository root:

```bash
# 0. Activate the local virtual environment
source venv/bin/activate

# 1. Run the offline unit/contract test suite (uses standard library unittest; no pytest needed)
python -m unittest discover -s tests -v

# 2. Run the static dry-run on the agent package (sitecustomize.py auto-adds BE/ to sys.path)
python -m agent --dry-run

# 3. Run the static dry-run on the RAG ingestion package
python -m rag.ingestion --dry-run
```

* **Junior expectation:** *"90 tests pass and `--dry-run` says OK. The system works!"*
* **Senior IC mindset:** *"Static tests use dependency injection and mocked tools. `--dry-run` doesn't open a single network socket. Now I will test every boundary individually."*

---

## Phase 1: The Wire Contract & Entry Point (FE ↔ BFF)

**Objective:** Prove that the Frontend SSE parser and Backend SSE frame generator agree on the wire format, and understand how the BFF server boots.

### Step 1.1 — Inspect the Frontend SSE State Machine
* **Files to open:**
  * [`FE/src/features/dispatch/hooks/useDispatchStream.js`](../../FE/src/features/dispatch/hooks/useDispatchStream.js) (lines 13–145)
  * [`FE/src/shared/api/sseClient.js`](../../FE/src/shared/api/sseClient.js)
  * [`FE/src/shared/api/endpoints.js`](../../FE/src/shared/api/endpoints.js)
* **Checklist to verify in code:**
  * [ ] Why does `sseClient.js` use `fetch` instead of browser `EventSource`? *(Because `EventSource` only supports `GET`, and `/api/v1/dispatch/stream` requires a `POST` body `{session_id, prompt}`).*
  * [ ] In `useDispatchStream.js`, look at `onEvent({ type, data })`: verify the exact 6 event types handled (`status`, `tool_input`, `tool_output`, `final_answer`, `error`, `done`).
  * [ ] Notice how `placeholderAssistant` starts with `state: "running"` and appends every `tool_input` and `tool_output` into `m.traces[]`.

### Step 1.2 — Inspect & Run the Backend SSE Frame Generator
* **File to open:** [`BE/api/server.py`](../../BE/api/server.py) (lines 28–169, `generate_dispatch_sse_frames`)
* **Run this verification command** to test the pure SSE frame contract in isolation (no LLM or DB required):

```bash
python -m unittest tests.test_fe_api -v
```

* **Run a live one-liner** to inspect the raw wire frames produced by `generate_dispatch_sse_frames` when a streamer yields a normal sequence vs. when a streamer yields nothing:

```bash
PYTHONPATH=BE python -c '
from api.server import generate_dispatch_sse_frames

print("=== CASE A: Normal Stream ===")
fake_stream = lambda sid, p: [
    {"type": "tool_input", "name": "query_telemetry_db", "args": {"sql_query": "SELECT 1"}},
    {"type": "tool_output", "name": "query_telemetry_db", "content": "1"},
    {"type": "final_answer", "content": "All clear."}
]
for frame in generate_dispatch_sse_frames("s1", "hello", fake_stream, lambda *a: None):
    print(frame.strip())

print("\n=== CASE B: Empty Stream (No final_answer) ===")
for frame in generate_dispatch_sse_frames("s1", "hello", lambda s, p: [], lambda *a: None):
    print(frame.strip())
'
```

* **Expected Output:**
  * Case A emits `event: status` → `event: tool_input` → `event: tool_output` → `event: final_answer` → `event: done`.
  * Case B emits `event: status` → `event: error` (Execution Timeout) → `event: done`.
* **Senior IC Catch #1 (Dual Server Drift):** Scroll down in [`BE/api/server.py`](../../BE/api/server.py) to line 427 (`create_app` FastAPI) vs. line 570 (`run_stdlib_server`). Notice there are **two** web servers in the same file. Compare `/api/v1/meta` in both: the stdlib server is missing fields (`capabilities`), and `/api/v1/meta` uses raw `os.getenv` instead of `AgentSettings`.

---

## Phase 2: The Brain (LangGraph, Prompts, & Session Memory)

**Objective:** Verify how the LangGraph state machine compiles, which LLM it binds, how the system prompt shapes output, and how conversation state is stored.

### Step 2.1 — Inspect Graph Topology & LLM Resolution
* **Files to open:**
  * [`BE/agent/graph.py`](../../BE/agent/graph.py) (lines 64–145, `build_reasoner_llm` and `build_agent_graph`)
  * [`BE/agent/config.py`](../../BE/agent/config.py)
* **Run this verification script** to verify what `AgentSettings` resolves from `.env` vs. what `/api/v1/meta` reports:

```bash
PYTHONPATH=BE python -c '
import os
from agent.config import get_agent_settings
cfg = get_agent_settings()
print("AgentSettings LLM :", cfg.agent_llm)
print("Gemini Model      :", cfg.gemini_model)
print("Gemini Key Set?   :", bool(cfg.gemini_api_key and cfg.gemini_api_key.get_secret_value()))
print("System Prompt Path:", cfg.system_prompt_path, "exists=", cfg.system_prompt_path.exists())
'
```

* **Expected Output:** Prints the resolved provider (`GEMINI` / `OPENAI` / `OLLAMA`), model name, whether the API key is populated, and confirms [`BE/agent/prompts/system_prompt.txt`](../../BE/agent/prompts/system_prompt.txt) exists.
* **Senior IC Catch #2 (Unbounded ReAct Loop & Orchestrator Bypass):**
  1. Look at [`BE/agent/graph.py`](../../BE/agent/graph.py) lines 147–240 (`AgentOrchestrator`). It audits the user prompt (`USER_PROMPT`) and attaches the Langfuse callback handler.
  2. Now look at [`BE/api/server.py`](../../BE/api/server.py) line 376 (`default_agent_streamer`). It does **not** use `AgentOrchestrator`! It calls `_get_compiled_graph().stream(...)` directly—meaning user prompts are never audited on the BFF path, and `recursion_limit` is never set.
  3. Look at `_get_compiled_graph()` in [`BE/api/server.py`](../../BE/api/server.py) (lines 291–305): if `build_agent_graph()` raises a config error, it silently falls back to `from src.orchestrator import fde_agent` (legacy code), hiding your configuration mistake!

### Step 2.2 — Test Session Memory & `/session/purge`
* **File to open:** [`BE/api/server.py`](../../BE/api/server.py) (search for `purge_session`)
* **Run this check** to see what `/api/v1/session/purge` actually does:

```bash
PYTHONPATH=BE python -c '
from fastapi.testclient import TestClient
from api.server import create_app

client = TestClient(create_app(agent_streamer=lambda s, p: [], audit_writer=lambda *a: None))
res = client.post("/api/v1/session/purge", json={"old_session_id": "thread-123"})
print("Purge response:", res.json())
'
```

* **Expected Output:** Returns `{"status": "purged", "old_session_id": "thread-123", "new_session_id": "session-..."}`.
* **Senior IC Catch #3 (Memory Leak on Purge):** Check the implementation of `purge_session` in `BE/api/server.py`. It only generates a new UUID! It never clears `old_session_id` from LangGraph’s `MemorySaver` checkpointer. In a long-running process, old thread states accumulate in RAM forever.

---

## Phase 3: Tools, Guardrails, & Data Contracts

**Objective:** Test each of the 3 LangGraph tools (`query_telemetry_db`, `fetch_corridor_conditions`, `search_compliance_sop`) in isolation and verify their security and schema contracts.

### Step 3.1 — Verify Tool 1: `query_telemetry_db` & SQL Guardrails
* **Files to open:**
  * [`BE/agent/tools/telemetry_sql.py`](../../BE/agent/tools/telemetry_sql.py)
  * [`BE/sql/setup_security_view.sql`](../../BE/sql/setup_security_view.sql)
  * [`BE/agent/prompts/system_prompt.txt`](../../BE/agent/prompts/system_prompt.txt)
* **Run the SQL guardrail verification script** (offline check + live DB check):

```bash
PYTHONPATH=BE python -c '
from agent.tools.telemetry_sql import validate_readonly_select, UnsafeSqlError, make_telemetry_sql_tool

# 1. Verify guardrails block attacks and legacy table access
for bad_sql in [
    "DROP TABLE dbo.TBL_SC_FLEET_HIST_RAW",
    "SELECT * FROM dbo.TBL_SC_FLEET_HIST_RAW",
    "SELECT * FROM FDE_VIEWS.VW_ACTIVE_FLEET; SELECT 1",
    "SELECT * FROM FDE_VIEWS.VW_ACTIVE_FLEET -- comment",
]:
    try:
        validate_readonly_select(bad_sql)
        print("FAIL - Should have blocked:", bad_sql)
    except UnsafeSqlError as e:
        print("PASS - Blocked as expected:", e)

# 2. Test live DB connection via the tool
tool = make_telemetry_sql_tool()
out = tool.invoke({"sql_query": "SELECT TOP 1 Timestamp, Latitude, Longitude, Current_Temperature_C FROM FDE_VIEWS.VW_ACTIVE_FLEET"})
print("\nLive SQL Tool Output:\n", out)
'
```

* **Expected Output:**
  * All 4 malicious/unauthorized queries print `PASS - Blocked as expected: SECURITY BLOCK: ...`.
  * Live SQL Tool Output prints either `COLUMNS: Timestamp, ...` (if MSSQL + ODBC Driver 18 are up) or `Database Error: ...` (note that the tool returns a string rather than crashing the agent).
* **Senior IC Catch #4 (The Schema Alias Trap):** Compare [`BE/sql/setup_security_view.sql`](../../BE/sql/setup_security_view.sql) lines 11–22 with [`BE/api/server.py`](../../BE/api/server.py) lines 316–321:
  * The SQL view aliases `IOT_TEMP_VAL_C` → `Current_Temperature_C`, `PRT_CNG_LVL` → `Port_Congestion_Level`, `DELAY_PROB_DEC` → `Delay_Probability`.
  * Yet the SOP documents (and the canned fallback in `server.py`) refer to `IOT_TEMP_VAL_C`, `PRT_CNG_LVL`, and `DELAY_PROB_DEC`! If the LLM reads an SOP chunk with `IOT_TEMP_VAL_C` and puts `WHERE IOT_TEMP_VAL_C > 4.0` into `query_telemetry_db`, MSSQL will fail with `Invalid column name`. The system prompt needs an explicit column-mapping table.

### Step 3.2 — Verify Tool 2: `fetch_corridor_conditions` (Open-Meteo API)
* **File to open:** [`BE/agent/tools/corridor_api.py`](../../BE/agent/tools/corridor_api.py)
* **Run this check** to verify validation bounds and live HTTP weather retrieval:

```bash
PYTHONPATH=BE python -c '
from agent.tools.corridor_api import make_corridor_conditions_tool

tool = make_corridor_conditions_tool()
print("1. Bounds check:", tool.invoke({"latitude": 999.0, "longitude": -118.27}))
print("2. Live check:\n", tool.invoke({"latitude": 33.74, "longitude": -118.27, "forecast_days": 1}))
'
```

* **Expected Output:**
  * Check 1 returns `Corridor API Validation Error: coordinates (999.0, -118.27) are out of bounds...`.
  * Check 2 returns `--- LIVE CORRIDOR TELEMETRY ---` with live temperature, wind speed, and congestion index from Open-Meteo.

### Step 3.3 — Verify Tool 3: `search_compliance_sop` (In-Process RAG & Pinecone)
* **Files to open:**
  * [`BE/agent/tools/sop_rag.py`](../../BE/agent/tools/sop_rag.py)
  * [`BE/rag/config.py`](../../BE/rag/config.py)
* **Run this check** to inspect RAG configuration, cache manifests, and live SOP retrieval:

```bash
PYTHONPATH=BE python -c '
from pathlib import Path
from agent.tools.sop_rag import make_inprocess_sop_tool

manifests = list(Path("data/cache").glob("manifest__*.json"))
print("Ingestion manifests found:", manifests)

tool = make_inprocess_sop_tool()
res = tool.invoke({"query": "temperature breach above 4.0 C mitigation SOP"})
print("SOP Tool Output:\n", res)
'
```

* **Expected Output & Senior IC Catch #5 (Silent RAG Degradation + Cold-Start Latency):**
  * If `PINECONE_API_KEY` is missing in `.env` or ingestion hasn't run, the tool does **not** raise an error—it returns the sentinel string `SOP_KNOWLEDGE_BASE_UNAVAILABLE` (or `NO_MATCHING_SOP`).
  * Also notice in [`BE/agent/tools/sop_rag.py`](../../BE/agent/tools/sop_rag.py) lines 68–78 (`_get_service`): `RetrievalService.from_settings()` loads PyTorch and `BAAI/bge-m3` lazily **inside the first user request**, adding a multi-second cold-start spike on the first turn.

---

## Phase 4: Failure Semantics & Observability (The "Trust" Check)

**Objective:** Expose the critical compliance flaw (Finding F1) where a broken agent produces realistic fake answers, and verify the Audit Log + Trace store.

### Step 4.1 — Catch Finding F1 in Action (The Silent Fake Report)
* **File to open:** [`BE/api/server.py`](../../BE/api/server.py) (lines 308–421, `_offline_fallback_streamer` and `default_agent_streamer`)
* **Run this script** to see what happens when the LLM or graph fails during `default_agent_streamer`:

```bash
PYTHONPATH=BE python -c '
import api.server as srv

# Force _get_compiled_graph to raise a runtime error (simulating bad API key or network drop)
srv._ACTIVE_GRAPH = None
orig_get = srv._get_compiled_graph
srv._get_compiled_graph = lambda: (_ for _ in ()).throw(RuntimeError("GEMINI_API_KEY invalid!"))

try:
    events = list(srv.default_agent_streamer("test-session", "Are there any breaches near Miami?"))
    for e in events:
        print(e["type"], "->", e.get("name"), "|", str(e.get("content", e.get("args")))[:80])
finally:
    srv._get_compiled_graph = orig_get
'
```

* **Expected Output:** Instead of surfacing `RuntimeError("GEMINI_API_KEY invalid!")`, `default_agent_streamer` catches `Exception` on line 420 and yields 5 fabricated events from `_offline_fallback_streamer` reporting a **6.8°C breach at (33.74, -118.27)**!
* **Why this is a Senior IC P0 finding:**
  1. Look at line 418–421 in [`BE/api/server.py`](../../BE/api/server.py): even worse, if the real graph emits `tool_input` and `tool_output` and *then* throws an exception on the second LLM call, `except Exception:` yields the entire `_offline_fallback_streamer` **after** the real events!
  2. Both the real events and the fabricated events are passed to `writer_fn` in `generate_dispatch_sse_frames` and permanently written to `FDE_VIEWS.AgentAuditLog` as genuine compliance records.

### Step 4.2 — Verify Observability Spans vs. SQL Audit Log
* **Files to open:**
  * [`BE/api/server.py`](../../BE/api/server.py) (lines 177–286, `_build_mssql_engine`, `default_audit_writer`, `default_audit_reader`)
  * [`BE/observability/pipeline.py`](../../BE/observability/pipeline.py)
* **Run this check** to verify the in-memory trace ring buffer and audit writer behavior:

```bash
PYTHONPATH=BE python -c '
from api.server import generate_dispatch_sse_frames, get_langfuse_bridge

audit_rows = []
fake_writer = lambda sid, node, tool, content: audit_rows.append((sid, node, tool))
fake_streamer = lambda sid, p: [{"type": "final_answer", "content": "Test complete."}]

frames = list(generate_dispatch_sse_frames("obs-test-1", "Check prompt", fake_streamer, fake_writer))
spans = get_langfuse_bridge().query_spans(session_id_filter="obs-test-1")

print("Audit writer rows captured:", audit_rows)
print("Ring-buffer spans captured:", [(s["SessionID"], s["NodeExecuted"], s.get("TraceID")) for s in spans])
'
```

* **Senior IC Catch #6 (Audit Config & Correlation Gaps):**
  1. Notice `audit_rows` only has `('obs-test-1', 'reasoner_final', 'LLM Text Synthesis')`—the operator's input prompt (`"Check prompt"`) was never written to `writer_fn`.
  2. Look at `_build_mssql_engine` in [`BE/api/server.py`](../../BE/api/server.py) line 189: it hardcodes `DATABASE=master;` and reads raw `os.getenv("SQL_AGENT_PASSWORD", "")`, whereas [`BE/agent/config.py`](../../BE/agent/config.py) uses `AgentSettings.sql_database`.
  3. Notice that `spans` include `TraceID`, while `FDE_VIEWS.AgentAuditLog` has no `TraceID` column.

---

## Summary Scorecard: Junior vs. Senior Verification Matrix

Use this checklist as your personal sign-off before picking up repair PRs (Steps 0–4 in [`dispatch-request-flow.md`](./dispatch-request-flow.md)):

| Phase | Module Checked | Junior Check ("It runs") | Senior IC Verification ("It's trustworthy") | Status |
| :--- | :--- | :--- | :--- | :--- |
| **1. Wire & BFF** | `useDispatchStream.js`, `sseClient.js`, `BE/api/server.py` | `python -m unittest tests.test_fe_api` passes | Verified all 6 SSE frame types; identified FastAPI vs. stdlib BFF drift (`/meta`). | ☐ |
| **2. Agent & State** | `BE/agent/graph.py`, `BE/agent/config.py` | `python -m agent --dry-run` passes | Identified that BFF bypasses `AgentOrchestrator`, swallows build errors into legacy `src/`, lacks `recursion_limit`, and leaks memory on `/session/purge`. | ☐ |
| **3. Tools & Data** | `telemetry_sql.py`, `corridor_api.py`, `sop_rag.py` | Unit tests with mock engines/services pass | Tested each tool live; caught legacy SOP column names (`IOT_TEMP_VAL_C`) vs. view aliases (`Current_Temperature_C`); caught missing `PINECONE_API_KEY` and `bge-m3` first-request cold start. | ☐ |
| **4. Trust & Audit** | `BE/api/server.py`, `BE/observability/pipeline.py` | UI renders a report and Audit screen shows rows | Proved that an LLM/DB failure triggers `_offline_fallback_streamer`, mixing fake 6.8°C breaches into the UI and `AgentAuditLog` without a `simulated` flag. | ☐ |

---

## Next Action: Graduate from Onboarding by Shipping Step 0

Once you have run every snippet above and observed the outputs firsthand, your first contribution as a Senior-minded IC is **Step 0** from [`dispatch-request-flow.md`](./dispatch-request-flow.md):
* Add `python -m agent --smoke` and `GET /readyz` so that anyone on the team can run a single command that tests each live leg (LLM config, MSSQL view `SELECT TOP 1`, Open-Meteo HTTP, Pinecone SOP retrieval, and Audit `INSERT`) and prints an honest `PASS` / `FAIL` with the exact reason.
