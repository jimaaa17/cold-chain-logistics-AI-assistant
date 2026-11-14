"""HTTP API for the retrieval service.

    uvicorn rag.retrieval.api:app --host 0.0.0.0 --port 8001

Stateless: scale horizontally behind a load balancer. Keep it on the private network - per
the TDD only the Streamlit node (8501) is public; the agent reaches this service internally.
Note: with the LOCAL provider each uvicorn worker loads its own copy of the model (~2 GB for
bge-m3), so prefer more replicas with 1 worker each over many workers per replica.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from rag.retrieval.service import MAX_TOP_K, RetrievalService

logger = logging.getLogger(__name__)


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=MAX_TOP_K)
    filter: dict[str, Any] | None = Field(default=None, description="Pinecone metadata filter")
    min_score: float | None = Field(default=None, ge=-1.0, le=1.0)


class ChunkOut(BaseModel):
    id: str
    score: float
    text: str
    source_file: str | None
    metadata: dict[str, Any]


class RetrieveResponse(BaseModel):
    results: list[ChunkOut]
    took_ms: float


def _default_factory() -> RetrievalService:
    from rag.config import configure_logging, get_settings

    settings = get_settings()
    configure_logging(settings.rag_log_level)
    return RetrievalService.from_settings(settings)


def create_app(service_factory: Callable[[], RetrievalService] = _default_factory) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Load the model + connect once per process (warm start), not per request.
        app.state.service = service_factory()
        logger.info("Retrieval service ready")
        yield

    app = FastAPI(title="Cold-Chain SOP Retrieval Service", version="1.0.0", lifespan=lifespan)

    def _service(request: Request) -> RetrievalService:
        return request.app.state.service

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> dict[str, Any]:
        """Readiness: model loaded and Pinecone reachable."""
        service = _service(request)
        try:
            return {"status": "ready", **service.readiness(), "query_cache": service.cache_stats()}
        except Exception as exc:
            logger.warning("Readiness check failed: %s", exc)
            raise HTTPException(status_code=503, detail="vector store unreachable") from exc

    # Sync handler on purpose: embedding + Pinecone calls are blocking, FastAPI runs them in a threadpool.
    @app.post("/v1/retrieve", response_model=RetrieveResponse)
    def retrieve(body: RetrieveRequest, request: Request) -> RetrieveResponse:
        started = time.perf_counter()
        try:
            chunks = _service(request).retrieve(
                body.query, top_k=body.top_k, metadata_filter=body.filter, min_score=body.min_score
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Retrieval failed")
            raise HTTPException(
                status_code=503, detail="retrieval backend unavailable", headers={"Retry-After": "2"}
            ) from exc
        took_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info("retrieve top_k=%s hits=%d took_ms=%.1f", body.top_k, len(chunks), took_ms)
        return RetrieveResponse(results=[ChunkOut(**c.to_dict()) for c in chunks], took_ms=took_ms)

    return app


app = create_app()

