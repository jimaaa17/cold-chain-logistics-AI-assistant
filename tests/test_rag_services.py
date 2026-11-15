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


# ---- Code Review Findings (F-01 .. F-10) & Real SOP Asset Regression ------------------
def _cli_exit_code(argv: list[str]) -> int | str:
    from rag.ingestion import cli

    try:
        return cli.main(argv)
    except SystemExit as exc:
        return exc.code
    except Exception as exc:
        return f"crashed with {type(exc).__name__}"


class _PineconeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _FakeHuggingFaceEmbeddings:
    def __init__(self, **_):
        pass

    def embed_documents(self, texts):
        return [[0.1] * 1024 for _ in texts]

    def embed_query(self, text):
        return [0.1] * 1024


class _FakePineconeClient:
    def __init__(self, existing_dimension: int) -> None:
        self.indexes = {"fde-sop-index-local": existing_dimension}
        self.delete_kwargs: dict = {}

    def list_indexes(self):
        return SimpleNamespace(names=lambda: list(self.indexes))

    def describe_index(self, name):
        return SimpleNamespace(dimension=self.indexes[name], host=f"{name}.host")

    def delete_index(self, name, **kwargs):
        self.delete_kwargs = kwargs
        del self.indexes[name]

    def create_index(self, name, dimension, **_):
        self.indexes[name] = dimension

    def Index(self, host):
        return FakeIndex()


class TestCodeReviewFindings(RagTestCase):
    """Regression suite for F-01 through F-10 from materials/code-review/rag-code-review.pdf."""

    def test_F01_document_with_no_text_is_failed_and_previous_version_kept(self):
        doc = self.source / "sop.md"
        doc.write_text(SOP_MD)
        self.run_ingest()
        before = self.ids()

        doc.write_text("   \n\n  ")
        report, _ = self.run_ingest()

        self.assertIn("sop.md", report.failed)
        self.assertEqual(self.ids(), before)

    def test_F02_missing_pinecone_api_key_exits_2(self):
        from rag.ingestion import cli

        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(cli, "get_settings", lambda: Settings(_env_file=None)):
            self.assertEqual(_cli_exit_code(["--dry-run"]), 2)

    def test_F02_pinecone_auth_error_exits_2(self):
        from rag.ingestion import cli

        (self.source / "sop.md").write_text(SOP_MD)
        unauthorized = _PineconeError(401, "Invalid API key")
        with mock.patch.object(cli, "get_settings", lambda: self.settings()), \
             mock.patch("rag.core.embeddings.build_embedder", return_value=SimpleNamespace(dimension=DIM)), \
             mock.patch("rag.core.vector_store.connect_vector_store", side_effect=unauthorized):
            self.assertEqual(_cli_exit_code([]), 2)

    def test_F03_override_ignored_by_model_is_rejected_or_not_used(self):
        import sys
        from rag.core.embeddings import build_embedder

        settings = Settings(_env_file=None, pinecone_api_key="x", embeddings_provider="LOCAL",
                            local_embedding_device="cpu", embedding_dimensions=768)
        fake_module = SimpleNamespace(HuggingFaceEmbeddings=_FakeHuggingFaceEmbeddings)
        with mock.patch.dict(sys.modules, {"langchain_huggingface": fake_module}):
            embedder = build_embedder(settings)
        self.assertEqual(embedder.dimension, 1024)

    def test_F04_long_csv_row_respects_chunk_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "procedures.csv"
            path.write_text("step,procedure\n1," + "Restart the auxiliary cooling unit. " * 150 + "\n")
            chunks = parse_document(path, chunk_size=600, chunk_overlap=60)
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(max(len(c.text) for c in chunks), 600)
        self.assertTrue(all(c.text.startswith("step: 1 | ") for c in chunks))
        self.assertTrue(all(c.metadata.get("row_index") == 0 for c in chunks))

    def test_F05_invalid_worker_counts_are_rejected(self):
        from rag.ingestion import cli

        (self.source / "sop.md").write_text(SOP_MD)
        for workers in ("-1", "0", "500"):
            with self.subTest(workers=workers), mock.patch.object(cli, "get_settings", lambda: self.settings()):
                self.assertEqual(_cli_exit_code(["--workers", workers, "--dry-run"]), 2)

    def test_F08_delete_index_waits_for_deletion(self):
        import sys
        from rag.core.vector_store import connect_vector_store

        client = _FakePineconeClient(existing_dimension=1536)
        fake_module = SimpleNamespace(Pinecone=lambda api_key: client, ServerlessSpec=lambda **kw: kw)
        settings = Settings(_env_file=None, pinecone_api_key="x", allow_index_recreate=True)
        with mock.patch.dict(sys.modules, {"pinecone": fake_module}):
            connect_vector_store(settings, 1024, provision=True)
        self.assertNotIn("timeout", client.delete_kwargs)
        self.assertEqual(client.indexes["fde-sop-index-local"], 1024)

    def test_F10_plan_builds_a_constant_number_of_sets(self):
        files = {f"sop_{i}.md": Path(f"sop_{i}.md") for i in range(200)}
        settings = Settings(_env_file=None, pinecone_api_key="x")
        calls = 0

        def counting_set(*args):
            nonlocal calls
            calls += 1
            return set(*args)

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("rag.ingestion.pipeline.discover_documents", return_value=files), \
             mock.patch("rag.ingestion.pipeline.sha256_file", return_value="hash"), \
             mock.patch("rag.ingestion.pipeline.set", counting_set, create=True):
            build_plan(settings, Manifest(Path(tmp) / "m.json", "fp"), full_rebuild=True)
        self.assertLessEqual(calls, 5)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestCodeReviewApiFindings(RagTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.source / "sop.md").write_text(SOP_MD)
        self.run_ingest()
        self.http = self.client_for(self.index)

    def client_for(self, index) -> TestClient:
        from rag.retrieval.api import create_app

        service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(index, "sop"))
        client = TestClient(create_app(lambda: service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def tool_via_api(self):
        import httpx
        from rag.retrieval.client import RetrievalClient, make_sop_search_tool

        def forward(request: httpx.Request) -> httpx.Response:
            r = self.http.request(request.method, request.url.path, content=request.content,
                                  headers={"content-type": "application/json"})
            return httpx.Response(r.status_code, content=r.content, headers=r.headers)

        return make_sop_search_tool(RetrievalClient("http://rag", transport=httpx.MockTransport(forward)))

    def test_F06_invalid_filter_returns_4xx_not_503(self):
        with mock.patch.object(self.index, "query", side_effect=_PineconeError(400, "Invalid filter operator $badop")):
            response = self.http.post("/v1/retrieve",
                                      json={"query": "temperature", "filter": {"section": {"$badop": 1}}})
        self.assertIn(response.status_code, (400, 422))

    def test_F07_rejected_query_is_not_reported_as_knowledge_base_down(self):
        from rag.retrieval.client import QUERY_REJECTED_MESSAGE, UNAVAILABLE_MESSAGE

        answer = self.tool_via_api().invoke({"query": "temperature breach " * 150})
        self.assertNotEqual(answer, UNAVAILABLE_MESSAGE)
        self.assertEqual(answer, QUERY_REJECTED_MESSAGE)

    def test_F07_malformed_response_degrades_instead_of_raising(self):
        import httpx
        from rag.retrieval.client import UNAVAILABLE_MESSAGE, RetrievalClient, make_sop_search_tool

        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        tool = make_sop_search_tool(RetrievalClient("http://rag", transport=transport))
        answer = tool.invoke({"query": "temperature"})
        self.assertEqual(answer, UNAVAILABLE_MESSAGE)

    def test_F09_readyz_fails_when_namespace_is_empty(self):
        response = self.client_for(FakeIndex()).get("/readyz")
        self.assertEqual(response.status_code, 503)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestRealSopAssetEndToEnd(RagTestCase):
    """End-to-end regression test on data/policy/Cold_Chain_Incident_SOP_v2.md."""

    def test_real_sop_policy_ingest_and_retrieve_lifecycle(self):
        import httpx
        from rag.config import PROJECT_ROOT
        from rag.retrieval.api import create_app
        from rag.retrieval.client import RetrievalClient, make_sop_search_tool

        real_policy_dir = PROJECT_ROOT / "data" / "policy"
        real_sop = real_policy_dir / "Cold_Chain_Incident_SOP_v2.md"
        self.assertTrue(real_sop.is_file(), f"Missing real SOP asset at {real_sop}")

        settings = self.settings(sop_source_dir=real_policy_dir)
        report, manifest = self.run_ingest(settings)
        self.assertTrue(report.ok, f"Real SOP ingestion failed: {report.failed}")
        self.assertIn("Cold_Chain_Incident_SOP_v2.md", report.processed)
        self.assertEqual(report.chunks_upserted, 4)
        self.assertIn("Cold_Chain_Incident_SOP_v2.md", manifest.documents)

        service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(self.index, "sop"))
        with TestClient(create_app(lambda: service)) as http:
            ready = http.get("/readyz")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["vector_count"], 4)

            def forward(request: httpx.Request) -> httpx.Response:
                r = http.request(request.method, request.url.path, content=request.content,
                                 headers={"content-type": "application/json"})
                return httpx.Response(r.status_code, content=r.content, headers=r.headers)

            tool = make_sop_search_tool(RetrievalClient("http://rag", transport=httpx.MockTransport(forward)), top_k=3)
            output = tool.invoke({"query": "Fresh Perishables 4.0°C critical breach"})
            self.assertIn("Cold_Chain_Incident_SOP_v2.md", output)
            self.assertIn("Southern California Logistics Operations", output)


if __name__ == "__main__":
    unittest.main()
