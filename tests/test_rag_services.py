"""Offline tests for the RAG services (no network, no model downloads).

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rag.config import Settings
from rag.core.embeddings import Embedder
from rag.core.resilience import is_transient
from rag.core.vector_store import VectorStore
from rag.ingestion.manifest import Manifest
from rag.ingestion.parsers import DocumentParseError, parse_document
from rag.ingestion.pipeline import IngestionPipeline, build_plan, make_doc_key
from rag.retrieval.service import RetrievalService

DIM = 16


def setUpModule() -> None:
    logging.disable(logging.CRITICAL)  # expected-failure tests log tracebacks by design


def tearDownModule() -> None:
    logging.disable(logging.NOTSET)


class FakeBackend:
    def __init__(self) -> None:
        self.query_calls = 0

    @staticmethod
    def _vec(text: str) -> list[float]:
        digest = hashlib.sha256(" ".join(text.split()).encode()).digest()  # whitespace-insensitive, like real models
        return [b / 255 + 0.01 for b in digest[:DIM]]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return self._vec(text)


class FakeIndex:
    """In-memory stand-in implementing the subset of the Pinecone Index API we use."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict]] = defaultdict(dict)
        self.fail_queries = False

    def upsert(self, vectors, namespace=None):
        for v in vectors:
            assert all(ord(c) < 128 for c in v["id"]), "Pinecone IDs must be ASCII"
            assert None not in v["metadata"].values()
            self.data[namespace or ""][v["id"]] = v

    def list(self, prefix=None, namespace=None):
        ids = sorted(i for i in self.data[namespace or ""] if i.startswith(prefix or ""))
        for start in range(0, len(ids), 2):  # small pages to exercise pagination
            yield ids[start : start + 2]

    def delete(self, ids=None, namespace=None, **_):
        for i in ids:
            self.data[namespace or ""].pop(i, None)

    def query(self, vector, top_k, namespace=None, filter=None, **_):
        if self.fail_queries:
            raise ConnectionResetError("simulated outage")

        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))

        rows = list(self.data[namespace or ""].values())
        if filter and "source_file" in filter:
            rows = [r for r in rows if r["metadata"]["source_file"] == filter["source_file"]["$eq"]]
        scored = sorted(rows, key=lambda r: cosine(vector, r["values"]), reverse=True)[:top_k]
        return SimpleNamespace(matches=[
            SimpleNamespace(id=r["id"], score=cosine(vector, r["values"]), metadata=dict(r["metadata"]))
            for r in scored
        ])

    def describe_index_stats(self):
        return SimpleNamespace(namespaces={
            ns: SimpleNamespace(vector_count=len(v)) for ns, v in self.data.items()
        })


SOP_MD = """# Operations
## Cold-Chain SOP
### 1. Temperature Control
Fresh perishables must stay between 0.0C and 4.0C. Above 4.0C a breach is declared.
### 2. Route Congestion
If port congestion exceeds 7.0, divert to the Inland Empire Overflow Depot.
"""


class RagTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "policy"
        self.source.mkdir()
        self.index = FakeIndex()
        self.backend = FakeBackend()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def settings(self, **overrides) -> Settings:
        values = dict(pinecone_api_key="test", sop_source_dir=self.source,
                      rag_cache_dir=self.root / "cache", ingest_workers=2)
        values.update(overrides)
        return Settings(_env_file=None, **values)

    def run_ingest(self, settings: Settings | None = None, full_rebuild: bool = False):
        settings = settings or self.settings()
        manifest = Manifest(settings.manifest_path, settings.pipeline_fingerprint())
        embedder = Embedder(self.backend, "fake-model", dimension=DIM, batch_size=3)
        store = VectorStore(self.index, settings.pinecone_namespace, upsert_batch_size=2)
        plan = build_plan(settings, manifest, full_rebuild=full_rebuild)
        return IngestionPipeline(settings, embedder, store, manifest).run(plan), manifest

    def ids(self, namespace: str = "sop") -> list[str]:
        return sorted(self.index.data[namespace])


class TestIngestion(RagTestCase):
    def test_initial_ingest_then_unchanged_is_skipped(self):
        (self.source / "sop.md").write_text(SOP_MD)
        report, _ = self.run_ingest()
        self.assertTrue(report.ok)
        self.assertEqual(report.processed, ["sop.md"])
        self.assertGreater(len(self.ids()), 0)

        report, _ = self.run_ingest()
        self.assertEqual(report.processed, [])
        self.assertEqual(report.skipped, ["sop.md"])

    def test_shrunk_document_prunes_stale_chunks(self):
        doc = self.source / "long.txt"
        doc.write_text("\n\n".join(f"Paragraph {i}: " + "temperature log " * 30 for i in range(10)))
        self.run_ingest(self.settings(chunk_size=200, chunk_overlap=0))
        before = len(self.ids())

        doc.write_text("Only one short paragraph now.")
        report, _ = self.run_ingest(self.settings(chunk_size=200, chunk_overlap=0))
        self.assertGreater(before, 1)
        self.assertEqual(len(self.ids()), 1)
        self.assertEqual(report.stale_chunks_pruned, before - 1)

    def test_deleted_file_is_purged_from_index_and_manifest(self):
        (self.source / "a.md").write_text(SOP_MD)
        (self.source / "b.txt").write_text("keep me")
        self.run_ingest()
        (self.source / "a.md").unlink()
        report, manifest = self.run_ingest()
        self.assertEqual(report.deleted, ["a.md"])
        self.assertEqual(set(manifest.documents), {"b.txt"})
        self.assertTrue(all(i.startswith(make_doc_key("b.txt")) for i in self.ids()))

    def test_failed_update_keeps_previous_vectors_and_is_retried(self):
        doc = self.source / "sop.md"
        doc.write_text(SOP_MD)
        self.run_ingest()
        before = self.ids()

        doc.write_text(SOP_MD + "\n### 3. New section\nEscalate.")
        with mock.patch("rag.ingestion.pipeline.parse_document", side_effect=DocumentParseError("boom")):
            report, _ = self.run_ingest()
        self.assertFalse(report.ok)
        self.assertIn("sop.md", report.failed)
        self.assertEqual(self.ids(), before)  # old version still searchable

        report, _ = self.run_ingest()  # retried automatically on next run
        self.assertEqual(report.processed, ["sop.md"])

    def test_config_change_forces_reingest(self):
        (self.source / "sop.md").write_text(SOP_MD)
        self.run_ingest()
        report, _ = self.run_ingest(self.settings(chunk_size=300, chunk_overlap=30))
        self.assertEqual(report.processed, ["sop.md"])

    def test_one_bad_file_does_not_block_others(self):
        (self.source / "good.md").write_text(SOP_MD)
        (self.source / "bad.pdf").write_bytes(b"not a pdf")
        report, _ = self.run_ingest()
        self.assertEqual(report.processed, ["good.md"])
        self.assertIn("bad.pdf", report.failed)

    def test_legacy_ids_are_migrated_once(self):
        settings = self.settings()
        settings.rag_cache_dir.mkdir(parents=True)
        settings.legacy_cache_path.write_text(json.dumps({"sop.md": "oldhash"}))
        self.index.upsert([{"id": f"sop.md-chunk-{i}", "values": [0.1] * DIM, "metadata": {}} for i in range(3)])
        (self.source / "sop.md").write_text(SOP_MD)

        _, manifest = self.run_ingest(settings)
        self.assertEqual(self.ids(""), [])
        self.assertTrue(manifest.legacy_migrated)

    def test_ids_are_ascii_and_unique_for_unicode_names(self):
        self.assertNotEqual(make_doc_key("température.md"), make_doc_key("temp_rature.md"))
        self.assertTrue(make_doc_key("température.md").isascii())


class TestParsers(unittest.TestCase):
    def test_markdown_chunks_carry_section_breadcrumb(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sop.md"
            path.write_text(SOP_MD)
            chunks = parse_document(path, chunk_size=600, chunk_overlap=60)
        temp = next(c for c in chunks if "4.0C" in c.text)
        self.assertTrue(temp.text.startswith("Operations > Cold-Chain SOP > 1. Temperature Control"))
        self.assertEqual(temp.metadata["Header_3"], "1. Temperature Control")

    def test_csv_rows_skip_empty_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.csv"
            path.write_text("a,b\n1,\n,\n3,4\n")
            chunks = parse_document(path, chunk_size=600, chunk_overlap=60)
        self.assertEqual([c.text for c in chunks], ["a: 1", "a: 3 | b: 4"])


class TestRetrieval(RagTestCase):
    def make_service(self, **kwargs) -> RetrievalService:
        embedder = Embedder(self.backend, "fake-model", dimension=DIM)
        return RetrievalService(embedder, VectorStore(self.index, "sop"), **kwargs)

    def test_retrieve_ranks_exact_chunk_first_and_caches_query_embedding(self):
        (self.source / "sop.md").write_text(SOP_MD)
        self.run_ingest()
        target = next(v for v in self.index.data["sop"].values() if "4.0C" in v["metadata"]["text"])
        service = self.make_service()

        hits = service.retrieve(target["metadata"]["text"], top_k=2)
        self.assertEqual(hits[0].id, target["id"])
        self.assertAlmostEqual(hits[0].score, 1.0, places=5)
        self.assertNotIn("text", hits[0].metadata)

        service.retrieve("  " + target["metadata"]["text"] + "  ")  # whitespace-normalised cache hit
        self.assertEqual(self.backend.query_calls, 1)
        self.assertEqual(service.cache_stats()["hits"], 1)

    def test_min_score_and_empty_query(self):
        (self.source / "sop.md").write_text(SOP_MD)
        self.run_ingest()
        service = self.make_service()
        self.assertEqual(service.retrieve("anything", min_score=1.01), [])
        with self.assertRaises(ValueError):
            service.retrieve("   ")

    def test_transient_classification(self):
        self.assertTrue(is_transient(ConnectionResetError()))
        self.assertTrue(is_transient(type("E", (Exception,), {"status": 503})()))
        self.assertFalse(is_transient(type("E", (Exception,), {"status": 400})()))
        self.assertFalse(is_transient(ValueError("bad")))


class TestSettings(unittest.TestCase):
    def test_legacy_env_alias_and_per_provider_isolation(self):
        with mock.patch.dict(os.environ, {"Embeddings_model": " openai "}):
            openai = Settings(_env_file=None, pinecone_api_key="x")
        local = Settings(_env_file=None, pinecone_api_key="x", embeddings_provider="local")
        self.assertEqual(openai.embeddings_provider, "OPENAI")
        self.assertEqual(openai.index_name, "fde-sop-index-openai")
        self.assertEqual(local.index_name, "fde-sop-index-local")
        self.assertNotEqual(openai.manifest_path, local.manifest_path)
        self.assertNotEqual(openai.pipeline_fingerprint(), local.pipeline_fingerprint())

    def test_overlap_must_be_smaller_than_chunk(self):
        with self.assertRaises(ValueError):
            Settings(_env_file=None, pinecone_api_key="x", chunk_size=100, chunk_overlap=100)


try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestApiAndClient(RagTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.source / "sop.md").write_text(SOP_MD)
        self.run_ingest()
        from rag.retrieval.api import create_app

        service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(self.index, "sop"))
        self.http = TestClient(create_app(lambda: service))
        self.http.__enter__()  # runs lifespan

    def tearDown(self) -> None:
        self.http.__exit__(None, None, None)
        super().tearDown()

    def test_endpoints(self):
        self.assertEqual(self.http.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(self.http.get("/readyz").json()["vector_count"], len(self.ids()))
        body = self.http.post("/v1/retrieve", json={"query": "temperature breach", "top_k": 2}).json()
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["results"][0]["source_file"], "sop.md")
        self.assertEqual(self.http.post("/v1/retrieve", json={"query": ""}).status_code, 422)

    def test_backend_outage_returns_503_and_tool_degrades_gracefully(self):
        import httpx

        from rag.retrieval.client import UNAVAILABLE_MESSAGE, RetrievalClient, make_sop_search_tool

        self.index.fail_queries = True
        response = self.http.post("/v1/retrieve", json={"query": "temperature"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "2")

        def forward(request: httpx.Request) -> httpx.Response:
            r = self.http.request(request.method, request.url.path, content=request.content,
                                  headers={"content-type": "application/json"})
            return httpx.Response(r.status_code, content=r.content, headers=r.headers)

        tool = make_sop_search_tool(RetrievalClient("http://rag", transport=httpx.MockTransport(forward)))
        self.assertEqual(tool.invoke({"query": "temperature"}), UNAVAILABLE_MESSAGE)

        self.index.fail_queries = False
        answer = tool.invoke({"query": "temperature"})
        self.assertIn("[1] (sop.md - Operations > Cold-Chain SOP", answer)


if __name__ == "__main__":
    unittest.main()
