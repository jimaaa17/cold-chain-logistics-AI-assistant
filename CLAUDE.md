# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Cold-Chain Logistics AI-Assistant: a natural-language assistant for control-tower operators to query fleet telemetry (legacy MSSQL) and compliance SOPs (Pinecone RAG). The target design is in `materials/Technical Design Document (TDD)_ Cold-Chain Logistics AI-Assistant.pdf`. It has three tiers:

- **Presentation**: a stateless Streamlit app, the only public ingress (port 8501).
- **Orchestration**: a LangGraph ReAct agent that classifies intent (Telemetry / SOP / Weather), picks tools and writes cited answers. It must degrade gracefully: when a tool fails it reports stale or missing data rather than inventing a status, and on a SQL error it retries a simpler query.
- **Data**: the agent reads telemetry only through the read-only view `VW_ACTIVE_FLEET` with the `USR_FDE_RO` account. Every decision cycle is appended to `AgentAuditLog` as `{Timestamp, User_Prompt, Tool_Invoked, Tool_Raw_Output, LLM_Decision}`. SQL (1433) is reachable only from the app node.

So far the repo contains the legacy-data loader and the SOP RAG services (`rag/`). The agent and the Streamlit UI are not built yet (branch `feat/langgraph-secure-agent`).

## Environment & commands

Python 3.12, local venv at `venv/`. Install with `pip install -r requirements.txt`. The `rag` package also imports `fastapi`, `uvicorn`, `httpx`, `tenacity` and `langchain-text-splitters`, which `requirements.txt` does not list; they are installed in `venv/` today. Configuration comes from `.env` at the repo root.

```bash
# Tests (unittest, fully offline: fake embedder + in-memory Pinecone index)
python -m unittest discover -s tests -v
python -m unittest tests.test_rag_services.TestIngestion            # one class
python -m unittest tests.test_rag_services.TestIngestion.test_shrunk_document_prunes_stale_chunks   # one test

# SOP ingestion (write path) → Pinecone
python -m rag.ingestion --dry-run        # show the plan only; loads no model, touches no index
python -m rag.ingestion                  # incremental sync of SOP_SOURCE_DIR (default data/policy/)
python -m rag.ingestion --full-rebuild [--allow-index-recreate]
# scripts/ingest_sop_pinecone.py is a backward-compatible shim for the same CLI

# Retrieval service (read path)
uvicorn rag.retrieval.api:app --host 0.0.0.0 --port 8001   # /healthz, /readyz, POST /v1/retrieve

# Legacy telemetry load: CSV (Kaggle URL in data/source/data.txt) → MSSQL dbo.TBL_SC_FLEET_HIST_RAW
python scripts/ingest_legacy_data.py     # needs ODBC Driver 18; CSV expected in data/raw/ (or RAW_DATA_PATH)
```

`docs/instructions.md` covers starting the MSSQL container (`mcr.microsoft.com/mssql/server:2022-latest` in Docker, locally or on EC2) and checking the load (expected 32,065 rows).

## Legacy data schema

`scripts/ingest_legacy_data.py` deliberately renames the clean dataset columns to "messy legacy" names: `TS_UTC, V_LAT, V_LON, IOT_TEMP_VAL_C, CGO_COND_CD, RISK_CLS_TXT, DELAY_PROB_DEC, PRT_CNG_LVL, RT_RSK_IDX, SYS_INGEST_FLAG`. The SOP (`data/policy/Cold_Chain_Incident_SOP_v2.md`) refers to these exact column names in its thresholds (for example `IOT_TEMP_VAL_C > 4.0`, `PRT_CNG_LVL > 7.0`, and High Risk with `DELAY_PROB_DEC > 0.65`), which ties the telemetry data and the SOP knowledge together. Keep the names consistent.

## RAG package architecture (`rag/`)

It is split along the read/write boundary so each side can be deployed separately:

- `rag/config.py`: a single pydantic-settings `Settings` object (env vars matched case-insensitively), read through the cached `get_settings()`. `EMBEDDINGS_PROVIDER` is `LOCAL` (default, `BAAI/bge-m3`, 1024-dim) or `OPENAI`. The index name defaults to `fde-sop-index-<provider>`.
- `rag/core/`: shared pieces. `Embedder` adds batching, retries, a dimension check and, for local torch models, a lock that serialises calls. `VectorStore` wraps one Pinecone index and namespace. `with_retry` retries only transient errors (429/5xx/timeouts). The models are plain dataclasses.
- `rag/ingestion/`: an offline batch job, meant to run with concurrency 1.
  - **Manifest** (`data/cache/manifest__<index>__<namespace>.json`): records the sha256 and *pipeline fingerprint* of each file. The fingerprint hashes the provider, model, dimensions, chunking, document_type and `PIPELINE_VERSION`. **Bump `PIPELINE_VERSION` in `config.py` whenever parsing, chunking or metadata output changes**, so every document gets re-embedded.
  - Vector IDs are deterministic: `<doc_key>#<00000>`. This allows deletion by prefix listing (serverless indexes don't support delete-by-filter).
  - A document is upserted first and its stale IDs are pruned afterwards. A failure in one document never aborts the run, and that document is not recorded, so the next run retries it. Exit codes: 0 = ok, 1 = some docs failed, 2 = config error.
  - Parsers are registered by extension with `@register(".ext")` in `parsers.py` (md, txt, pdf, csv, xlsx). They raise `DocumentParseError` instead of returning `[]`. Markdown chunks get their header breadcrumb prepended to the embedded text.
  - Only ingestion may create an index (`provision=True`). If the index dimension doesn't match the model, it is dropped only when `ALLOW_INDEX_RECREATE` is set.
- `rag/retrieval/`: the online read path.
  - `service.py`: transport-independent logic with an LRU cache of query embeddings.
  - `api.py`: a stateless FastAPI app. Use `create_app(service_factory)` to inject a fake in tests. Keep it on the private network.
  - `client.py`: the agent-side `RetrievalClient` and `make_sop_search_tool()`, a LangChain tool. It is kept deliberately light (no torch, no Pinecone SDK or credentials; it needs only `RETRIEVAL_SERVICE_URL`). When the backend fails or nothing matches, it returns the sentinel strings `SOP_KNOWLEDGE_BASE_UNAVAILABLE` / `NO_MATCHING_SOP` instead of raising, so the LLM doesn't answer from memory.

Heavy imports (torch, pinecone, langchain backends) are deferred inside functions so that `--help` and `--dry-run` start instantly and the tests run offline. Keep that pattern.
