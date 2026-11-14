"""Reproductions of the open findings from the rag/ code review (materials/code-review/).

Each test asserts the *correct* behaviour, so it FAILS while the finding is open and passes
once it is fixed. The file name deliberately does not match the default ``test*.py`` pattern,
so ``python -m unittest discover -s tests`` stays green; run these explicitly:

    python -m unittest discover -s tests -p "review_findings_test.py" -v

When a finding is fixed, move its test into test_rag_services.py as a regression test.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from rag.config import Settings
from rag.core.embeddings import build_embedder
from rag.core.vector_store import VectorStore, connect_vector_store
from rag.ingestion import cli
from rag.ingestion.manifest import Manifest
from rag.ingestion.parsers import parse_document
from rag.ingestion.pipeline import build_plan
from rag.retrieval.api import create_app
from rag.retrieval.client import UNAVAILABLE_MESSAGE, RetrievalClient, make_sop_search_tool
from rag.retrieval.service import RetrievalService
from rag.core.embeddings import Embedder
from test_rag_services import DIM, SOP_MD, FakeIndex, RagTestCase, setUpModule, tearDownModule  # noqa: F401


def exit_code(argv: list[str]) -> int | str:
    """Run the ingestion CLI and return its exit code, or a description of the crash."""
    try:
        return cli.main(argv)
    except SystemExit as exc:  # argparse rejects bad arguments with exit code 2
        return exc.code
    except Exception as exc:
        return f"crashed with {type(exc).__name__}"


class PineconeError(Exception):
    """Stand-in for a Pinecone API exception carrying an HTTP status (as the real SDK does)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Finding01_EmptyDocumentWipesIndex(RagTestCase):
    def test_document_with_no_text_is_failed_and_previous_version_kept(self):
        doc = self.source / "sop.md"
        doc.write_text(SOP_MD)
        self.run_ingest()
        before = self.ids()

        doc.write_text("   \n\n  ")  # e.g. a scanned PDF, or an SOP saved blank by mistake
        report, manifest = self.run_ingest()

        self.assertIn("sop.md", report.failed, "a document with no text must be reported as failed")
        self.assertEqual(self.ids(), before, "the previous, searchable version must be kept")


class Finding02_ConfigErrorsExitWith2(RagTestCase):
    def test_missing_pinecone_api_key_exits_2(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(cli, "get_settings", lambda: Settings(_env_file=None)):
            self.assertEqual(exit_code(["--dry-run"]), 2)

    def test_pinecone_auth_error_exits_2(self):
        (self.source / "sop.md").write_text(SOP_MD)
        unauthorized = PineconeError(401, "Invalid API key")
        with mock.patch.object(cli, "get_settings", lambda: self.settings()), \
             mock.patch("rag.core.embeddings.build_embedder", return_value=SimpleNamespace(dimension=DIM)), \
             mock.patch("rag.core.vector_store.connect_vector_store", side_effect=unauthorized):
            self.assertEqual(exit_code([]), 2)


class FakeHuggingFaceEmbeddings:
    """bge-m3 always produces 1024-dim vectors, whatever EMBEDDING_DIMENSIONS says."""

    def __init__(self, **_):
        pass

    def embed_documents(self, texts):
        return [[0.1] * 1024 for _ in texts]

    def embed_query(self, text):
        return [0.1] * 1024


class Finding03_EmbeddingDimensionsOverride(unittest.TestCase):
    def test_override_ignored_by_model_is_rejected_or_not_used(self):
        settings = Settings(_env_file=None, pinecone_api_key="x", embeddings_provider="LOCAL",
                            local_embedding_device="cpu", embedding_dimensions=768)
        fake_module = SimpleNamespace(HuggingFaceEmbeddings=FakeHuggingFaceEmbeddings)
        with mock.patch.dict(sys.modules, {"langchain_huggingface": fake_module}):
            try:
                embedder = build_embedder(settings)
            except ValueError:
                return  # rejecting the config up front is an acceptable fix
        self.assertEqual(embedder.dimension, 1024,
                         "the index would be provisioned at 768 dims while the model emits 1024")


class Finding04_TableRowsNotSplit(unittest.TestCase):
    def test_long_csv_row_respects_chunk_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "procedures.csv"
            path.write_text("step,procedure\n1," + "Restart the auxiliary cooling unit. " * 150 + "\n")
            chunks = parse_document(path, chunk_size=600, chunk_overlap=60)
        self.assertLessEqual(max(len(c.text) for c in chunks), 600)


class Finding05_WorkersNotValidated(RagTestCase):
    def test_invalid_worker_counts_are_rejected(self):
        (self.source / "sop.md").write_text(SOP_MD)
        for workers in ("-1", "0", "500"):
            with self.subTest(workers=workers), mock.patch.object(cli, "get_settings", lambda: self.settings()):
                self.assertEqual(exit_code(["--workers", workers, "--dry-run"]), 2)


class ApiTestCase(RagTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.source / "sop.md").write_text(SOP_MD)
        self.run_ingest()
        self.http = self.client_for(self.index)

    def client_for(self, index) -> TestClient:
        service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(index, "sop"))
        client = TestClient(create_app(lambda: service))
        client.__enter__()  # runs lifespan
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def tool_via_api(self):
        def forward(request: httpx.Request) -> httpx.Response:
            r = self.http.request(request.method, request.url.path, content=request.content,
                                  headers={"content-type": "application/json"})
            return httpx.Response(r.status_code, content=r.content, headers=r.headers)

        return make_sop_search_tool(RetrievalClient("http://rag", transport=httpx.MockTransport(forward)))


class Finding06_BadFilterReportedAsOutage(ApiTestCase):
    def test_invalid_filter_returns_4xx_not_503(self):
        with mock.patch.object(self.index, "query", side_effect=PineconeError(400, "Invalid filter operator $badop")):
            response = self.http.post("/v1/retrieve",
                                      json={"query": "temperature", "filter": {"section": {"$badop": 1}}})
        self.assertIn(response.status_code, (400, 422), "a caller error must not look like an outage")


class Finding07_ClientMisreportsErrors(ApiTestCase):
    def test_rejected_query_is_not_reported_as_knowledge_base_down(self):
        answer = self.tool_via_api().invoke({"query": "temperature breach " * 150})  # > 2000 chars -> 422
        self.assertNotEqual(answer, UNAVAILABLE_MESSAGE)

    def test_malformed_response_degrades_instead_of_raising(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        tool = make_sop_search_tool(RetrievalClient("http://rag", transport=transport))
        try:
            answer = tool.invoke({"query": "temperature"})
        except Exception as exc:
            self.fail(f"tool raised {type(exc).__name__} instead of degrading gracefully")
        self.assertEqual(answer, UNAVAILABLE_MESSAGE)


class FakePineconeClient:
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


class Finding08_RecreateRace_Withdrawn(unittest.TestCase):
    """Not a bug: pinecone 7.3.0 ``delete_index(name)`` with the default ``timeout=None`` blocks
    until the index is gone, so the following ``create_index`` cannot hit a name conflict.
    This PASSING test pins that assumption: it breaks if someone adds ``timeout=-1``."""

    def test_delete_index_waits_for_deletion(self):
        client = FakePineconeClient(existing_dimension=1536)
        fake_module = SimpleNamespace(Pinecone=lambda api_key: client, ServerlessSpec=lambda **kw: kw)
        settings = Settings(_env_file=None, pinecone_api_key="x", allow_index_recreate=True)
        with mock.patch.dict(sys.modules, {"pinecone": fake_module}):
            connect_vector_store(settings, 1024, provision=True)
        self.assertNotIn("timeout", client.delete_kwargs)
        self.assertEqual(client.indexes["fde-sop-index-local"], 1024)


class Finding09_ReadyWithEmptyIndex(ApiTestCase):
    def test_readyz_fails_when_namespace_is_empty(self):
        response = self.client_for(FakeIndex()).get("/readyz")
        self.assertEqual(response.status_code, 503, "an empty namespace answers every query with no hits")


class Finding10_QuadraticPlan(unittest.TestCase):
    def test_plan_builds_a_constant_number_of_sets(self):
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
        self.assertLessEqual(calls, 5, f"set() built {calls} times for {len(files)} files: O(n^2)")


if __name__ == "__main__":
    unittest.main()
