"""Agent-side client for the retrieval service.

Deliberately lightweight: no torch, no Pinecone SDK, no Pinecone credentials. The LangGraph
orchestrator only needs ``RETRIEVAL_SERVICE_URL``, which keeps the agent process small and the
vector-store credentials confined to the data tier.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8001"

UNAVAILABLE_MESSAGE = (
    "SOP_KNOWLEDGE_BASE_UNAVAILABLE: the compliance SOP lookup failed. Tell the operator the SOP "
    "data could not be retrieved right now. Do NOT answer procedure questions from memory."
)
NO_MATCH_MESSAGE = "NO_MATCHING_SOP: no SOP passage matched this question. Say so instead of guessing."


class RetrievalUnavailable(RuntimeError):
    pass


class RetrievalClient:
    def __init__(self, base_url: str | None = None, *, timeout_s: float | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        base_url = base_url or os.getenv("RETRIEVAL_SERVICE_URL", DEFAULT_URL)
        timeout_s = timeout_s or float(os.getenv("RETRIEVAL_TIMEOUT_S", "5"))
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s, connect=2.0),
            # Retries connection failures only (safe: retrieval is idempotent, but don't stack
            # latency on slow responses). Connection pooling keeps per-call overhead low.
            transport=transport or httpx.HTTPTransport(retries=2),
        )

    def search(self, query: str, *, top_k: int | None = None,
               metadata_filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query}
        if top_k:
            payload["top_k"] = top_k
        if metadata_filter:
            payload["filter"] = metadata_filter
        try:
            response = self._client.post("/v1/retrieve", json=payload)
            response.raise_for_status()
            return response.json()["results"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RetrievalUnavailable(str(exc)) from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RetrievalClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def format_results(results: list[dict[str, Any]]) -> str:
    """Render hits as numbered, cited excerpts the LLM can quote (TDD 2.1 step 4: cited answers)."""
    blocks = []
    for i, hit in enumerate(results, start=1):
        meta = hit.get("metadata", {})
        location = meta.get("section") or (f"page {meta['page_number']}" if "page_number" in meta else "")
        cite = f"{hit.get('source_file')}" + (f" - {location}" if location else "")
        blocks.append(f"[{i}] ({cite}; relevance {hit['score']:.2f})\n{hit['text']}")
    return "\n\n".join(blocks)


def make_sop_search_tool(client: RetrievalClient | None = None, *, top_k: int = 5):
    """LangChain tool for the LangGraph ReAct agent, with graceful degradation (TDD 2.2)."""
    from langchain_core.tools import tool

    client = client or RetrievalClient()

    @tool("search_cold_chain_sops")
    def search_cold_chain_sops(query: str) -> str:
        """Search the cold-chain compliance SOP knowledge base. Use for questions about temperature
        thresholds, breach mitigation, port-congestion diversion, risk escalation and other operating
        procedures. Returns numbered excerpts with their source document for citation."""
        try:
            results = client.search(query, top_k=top_k)
        except RetrievalUnavailable:
            return UNAVAILABLE_MESSAGE
        return format_results(results) if results else NO_MATCH_MESSAGE

    return search_cold_chain_sops

