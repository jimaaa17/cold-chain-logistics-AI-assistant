"""In-process SOP compliance retrieval tool backed by `rag.retrieval.service.RetrievalService`.

Calls ``RetrievalService`` directly in-process (no separate HTTP server required) while
preserving all ``rag/`` guarantees:
- Lazy initialization (importing ``agent`` never loads PyTorch or connects to Pinecone at import time).
- Multimodal citation formatting (Markdown section breadcrumbs, PDF page numbers, image modality tags).
- Graceful degradation sentinels (``SOP_KNOWLEDGE_BASE_UNAVAILABLE``, ``SOP_QUERY_REJECTED``,
  ``NO_MATCHING_SOP``).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from langchain_core.tools import tool

from rag.core.models import RetrievedChunk
from rag.retrieval.client import (
    NO_MATCH_MESSAGE,
    QUERY_REJECTED_MESSAGE,
    UNAVAILABLE_MESSAGE,
)
from rag.retrieval.service import RetrievalService

if TYPE_CHECKING:
    from agent.config import AgentSettings
    from rag.config import Settings as RagSettings

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 2000


def format_inprocess_results(chunks: list[RetrievedChunk]) -> str:
    """Format RetrievedChunk objects into cited context blocks including multimodal metadata."""
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        meta = chunk.metadata or {}
        modality = meta.get("modality")
        section = meta.get("section")
        page = f"page {meta['page_number']}" if "page_number" in meta else ""
        row = f"row {meta['row_index']}" if "row_index" in meta else ""

        loc_parts = [p for p in (section, page, row, f"modality={modality}" if modality else "") if p]
        location = " | ".join(loc_parts)
        source = chunk.source_file or "SOP"
        cite = f"{source} - {location}" if location else source
        blocks.append(f"[{i}] ({cite}; relevance {chunk.score:.2f})\n{chunk.text}")
    body = "\n\n".join(blocks)
    return f"--- COMPLIANCE SOP CONTEXT ---\n{body}\n------------------------------"


def make_inprocess_sop_tool(
    service: RetrievalService | Callable[[], RetrievalService] | None = None,
    *,
    rag_settings: "RagSettings | None" = None,
    agent_settings: "AgentSettings | None" = None,
    top_k: int | None = None,
):
    """Create the `search_compliance_sop` LangChain tool backed by in-process `RetrievalService`."""
    from agent.config import get_agent_settings

    cfg = agent_settings or get_agent_settings()
    effective_top_k = top_k or cfg.rag_top_k
    _service_instance: RetrievalService | None = service if isinstance(service, RetrievalService) else None
    _service_factory: Callable[[], RetrievalService] | None = service if callable(service) else None

    def _get_service() -> RetrievalService:
        nonlocal _service_instance
        if _service_instance is None:
            if _service_factory is not None:
                _service_instance = _service_factory()
            else:
                from rag.config import get_settings

                r_settings = rag_settings or get_settings()
                _service_instance = RetrievalService.from_settings(r_settings)
        return _service_instance

    @tool("search_compliance_sop")
    def search_compliance_sop(query: str) -> str:
        """Searches enterprise Standard Operating Procedures (SOPs) indexed in the Pinecone Vector DB
        (including Markdown, PDF, spreadsheet, and multimodal image SOPs).
        Use this to retrieve regulatory thresholds, cold-chain breach mitigations, and rerouting rules."""
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            return QUERY_REJECTED_MESSAGE
        try:
            srv = _get_service()
            chunks = srv.retrieve(query, top_k=effective_top_k)
        except ValueError:
            return QUERY_REJECTED_MESSAGE
        except Exception as exc:
            logger.warning("In-process SOP retrieval failed: %s", exc)
            return UNAVAILABLE_MESSAGE

        if not chunks:
            return NO_MATCH_MESSAGE
        return format_inprocess_results(chunks)

    return search_compliance_sop
