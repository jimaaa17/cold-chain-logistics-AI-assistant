"""Offline unit and end-to-end tests for:
1. Multimodal SOP ingestion in `rag/` (images .png/.jpg/.webp and scanned/hybrid PDFs).
2. Refactored backend `agent/` package (config, SQL guard, corridor API, in-process RAG tool,
   LangGraph ReAct orchestrator, and immutable SQL audit logging).
3. Verification that `src/` and `scripts/ingest_sop_pinecone.py` remain untouched.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from langchain_core.messages import AIMessage

from agent import cli as agent_cli
from agent.audit import AuditLogger
from agent.config import PROJECT_ROOT, AgentSettings
from agent.graph import AgentOrchestrator
from agent.tools.corridor_api import make_corridor_conditions_tool
from agent.tools.sop_rag import make_inprocess_sop_tool
from agent.tools.telemetry_sql import (
    UnsafeSqlError,
    make_telemetry_sql_tool,
    validate_readonly_select,
)
from rag.core.embeddings import Embedder
from rag.core.vector_store import VectorStore
from rag.ingestion.multimodal import set_multimodal_extractor
from rag.ingestion.parsers import DocumentParseError, parse_document, supported_extensions
from rag.retrieval.client import (
    NO_MATCH_MESSAGE,
    QUERY_REJECTED_MESSAGE,
    UNAVAILABLE_MESSAGE,
)
from rag.retrieval.service import RetrievalService
from test_rag_services import DIM, SOP_MD, FakeIndex, RagTestCase


def setUpModule() -> None:
    logging.disable(logging.CRITICAL)


def tearDownModule() -> None:
    set_multimodal_extractor(None)
    logging.disable(logging.NOTSET)


IMAGE_SOP_MARKDOWN = """# Visual Cold-Chain Escalation Matrix
## Reefer Unit Alarm Codes
### Code E-42: Auxiliary Compressor Fault
When reefer display flashes **E-42** and `IOT_TEMP_VAL_C` rises above **4.0°C**, engage backup power within 15 minutes and notify Tier 2 dispatch.
"""


class FakeMultimodalExtractor:
    """Deterministic offline Vision/OCR extractor for testing multimodal SOP ingestion."""

    def __init__(self, mapping: dict[str, str] | None = None, default_text: str = IMAGE_SOP_MARKDOWN) -> None:
        self.mapping = mapping or {}
        self.default_text = default_text
        self.calls: list[tuple[str, str]] = []

    def extract_image_bytes(self, image_bytes: bytes, mime_type: str, *, source_name: str = "") -> str:
        self.calls.append((source_name, mime_type))
        if source_name in self.mapping:
            return self.mapping[source_name]
        if image_bytes == b"BLANK_IMAGE":
            return "   "
        return self.default_text


class TestMultimodalRagIngestion(RagTestCase):
    def tearDown(self) -> None:
        set_multimodal_extractor(None)
        super().tearDown()

    def test_supported_extensions_include_images_and_documents(self):
        exts = supported_extensions()
        for expected in (".md", ".txt", ".pdf", ".csv", ".xlsx", ".png", ".jpg", ".jpeg", ".webp", ".tiff"):
            self.assertIn(expected, exts)

    def test_image_sop_ingested_with_section_breadcrumbs_and_retrieved(self):
        extractor = FakeMultimodalExtractor()
        set_multimodal_extractor(extractor)

        img_path = self.source / "alarm_matrix.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\nFAKE_REEFER_CHART")

        report, manifest = self.run_ingest()
        self.assertTrue(report.ok, f"Image SOP ingestion failed: {report.failed}")
        self.assertIn("alarm_matrix.png", report.processed)
        self.assertGreater(report.chunks_upserted, 0)
        self.assertEqual(extractor.calls, [("alarm_matrix.png", "image/png")])

        service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(self.index, "sop"))
        hits = service.retrieve("Code E-42 Auxiliary Compressor Fault 4.0°C", top_k=3)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].source_file, "alarm_matrix.png")
        self.assertEqual(hits[0].metadata.get("modality"), "image")
        self.assertIn("Visual Cold-Chain Escalation Matrix", hits[0].text)

    def test_blank_image_sop_fails_and_preserves_previous_vectors_F01(self):
        extractor = FakeMultimodalExtractor()
        set_multimodal_extractor(extractor)

        img_path = self.source / "flowchart.webp"
        img_path.write_bytes(b"VALID_WEBP_BYTES")
        report1, _ = self.run_ingest()
        self.assertTrue(report1.ok)
        before_ids = self.ids()
        self.assertGreater(len(before_ids), 0)

        # Overwrite with an unextractable / blank image -> must fail and preserve previous vectors (F-01)
        img_path.write_bytes(b"BLANK_IMAGE")
        report2, _ = self.run_ingest()
        self.assertFalse(report2.ok)
        self.assertIn("flowchart.webp", report2.failed)
        self.assertEqual(self.ids(), before_ids)

    def test_scanned_pdf_falls_back_to_multimodal_page_extractor(self):
        extractor = FakeMultimodalExtractor(
            mapping={"scanned_sop.pdf#page=1": "### Scanned Dock Procedure\nPre-cool trailer to -2.0°C before loading."}
        )
        set_multimodal_extractor(extractor)

        fake_image = SimpleNamespace(name="scan1.png", data=b"\x89PNG_SCAN")
        fake_page = SimpleNamespace(extract_text=lambda: "   ", images=[fake_image])
        fake_reader = SimpleNamespace(is_encrypted=False, pages=[fake_page])

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "scanned_sop.pdf"
            pdf_path.write_bytes(b"%PDF-1.7 fake scanned")
            with mock.patch("pypdf.PdfReader", return_value=fake_reader):
                chunks = parse_document(pdf_path, chunk_size=600, chunk_overlap=60)

        self.assertEqual(len(chunks), 1)
        self.assertIn("Pre-cool trailer to -2.0°C", chunks[0].text)
        self.assertEqual(chunks[0].metadata["page_number"], 1)
        self.assertEqual(chunks[0].metadata["modality"], "pdf_vision")


class FakeSqlResult:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows

    def keys(self):
        return self._columns

    def fetchmany(self, size: int):
        return self._rows[:size]


class FakeSqlEngine:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self.columns = columns
        self.rows = rows
        self.executed_sql: list[str] = []
        self.audit_rows: list[dict] = []

    def connect(self):
        engine = self

        class _Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, clause, params=None):
                engine.executed_sql.append(str(clause))
                return FakeSqlResult(engine.columns, engine.rows)

        return _Conn()

    def begin(self):
        engine = self

        class _Tx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, clause, params=None):
                engine.audit_rows.append(dict(params or {}))

        return _Tx()


class ScriptedToolCallingLLM:
    """Deterministic Reasoner LLM double that executes a scripted sequence of tool calls and answers."""

    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)
        self.invocations = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        idx = min(self.invocations, len(self._script) - 1)
        self.invocations += 1
        return self._script[idx]


class TestRefactoredAgentBackend(RagTestCase):
    def setUp(self) -> None:
        super().setUp()
        set_multimodal_extractor(FakeMultimodalExtractor())

    def tearDown(self) -> None:
        set_multimodal_extractor(None)
        super().tearDown()

    def test_sql_readonly_security_guard_blocks_unsafe_queries(self):
        valid = "SELECT TOP 5 Latitude, Longitude, Current_Temperature_C FROM FDE_VIEWS.VW_ACTIVE_FLEET;"
        self.assertEqual(
            validate_readonly_select(valid),
            "SELECT TOP 5 Latitude, Longitude, Current_Temperature_C FROM FDE_VIEWS.VW_ACTIVE_FLEET",
        )

        blocked_queries = [
            "DROP TABLE dbo.TBL_SC_FLEET_HIST_RAW",
            "UPDATE FDE_VIEWS.VW_ACTIVE_FLEET SET Current_Temperature_C = 2.0",
            "SELECT * FROM FDE_VIEWS.VW_ACTIVE_FLEET; DROP TABLE dbo.TBL_SC_FLEET_HIST_RAW",
            "SELECT * FROM FDE_VIEWS.VW_ACTIVE_FLEET -- comment injection",
            "SELECT TOP 5 * FROM dbo.TBL_SC_FLEET_HIST_RAW",  # raw table forbidden (TDD 3.1)
            "EXEC sp_who",
        ]
        for bad_sql in blocked_queries:
            with self.subTest(sql=bad_sql), self.assertRaises(UnsafeSqlError):
                validate_readonly_select(bad_sql)

    def test_telemetry_sql_tool_executes_valid_select_and_blocks_raw_table(self):
        fake_engine = FakeSqlEngine(
            columns=["Timestamp", "Latitude", "Longitude", "Current_Temperature_C", "Risk_Classification"],
            rows=[("2023-07-13 22:00", 33.77, -118.19, 6.4, "High Risk")],
        )
        tool = make_telemetry_sql_tool(AgentSettings(_env_file=None), engine=fake_engine)

        out = tool.invoke({"sql_query": "SELECT TOP 5 * FROM FDE_VIEWS.VW_ACTIVE_FLEET"})
        self.assertIn("COLUMNS: Timestamp, Latitude, Longitude, Current_Temperature_C, Risk_Classification", out)
        self.assertIn("6.4", out)

        blocked = tool.invoke({"sql_query": "SELECT TOP 5 * FROM dbo.TBL_SC_FLEET_HIST_RAW"})
        self.assertIn("SECURITY BLOCK", blocked)

    def test_corridor_conditions_tool_and_graceful_degradation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"current_weather": {"temperature": 28.5, "windspeed": 14.2}})

        tool = make_corridor_conditions_tool(
            AgentSettings(_env_file=None),
            transport=httpx.MockTransport(handler),
        )
        res = tool.invoke({"latitude": 33.8, "longitude": -118.1})
        self.assertIn("External Temp: 28.5°C | Wind Speed: 14.2 km/h", res)
        self.assertIn("High Transit Disruption (Congestion Index: 8.5/10)", res)

        # Out-of-bounds coordinates
        bad_coords = tool.invoke({"latitude": 120.0, "longitude": -118.1})
        self.assertIn("Validation Error", bad_coords)

        # Past + Forecast temporal window support
        def temporal_handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("past_days"), "2")
            self.assertEqual(request.url.params.get("forecast_days"), "3")
            return httpx.Response(
                200,
                json={
                    "current_weather": {"temperature": 24.0, "windspeed": 8.0},
                    "daily": {
                        "time": ["2026-11-13", "2026-11-14", "2026-11-15"],
                        "temperature_2m_max": [29.0, 26.5, 31.2],
                        "temperature_2m_min": [16.0, 15.2, 18.4],
                        "windspeed_10m_max": [7.5, 9.0, 16.4],
                    },
                },
            )

        temporal_tool = make_corridor_conditions_tool(
            AgentSettings(_env_file=None),
            transport=httpx.MockTransport(temporal_handler),
        )
        temporal_res = temporal_tool.invoke(
            {"latitude": 33.8, "longitude": -118.1, "past_days": 2, "forecast_days": 3}
        )
        self.assertIn("Temporal Window (past_days=2, forecast_days=3):", temporal_res)
        self.assertIn("2026-11-15: Temp 18.4°C..31.2°C | Max Wind 16.4 km/h (High Transit Disruption)", temporal_res)

        # Outage graceful degradation
        failing_tool = make_corridor_conditions_tool(
            AgentSettings(_env_file=None),
            transport=httpx.MockTransport(lambda req: httpx.Response(503)),
        )
        outage_res = failing_tool.invoke({"latitude": 33.8, "longitude": -118.1})
        self.assertIn("Corridor API Communication Failure", outage_res)

    def test_inprocess_sop_tool_retrieves_markdown_and_multimodal_sops(self):
        real_sop = (PROJECT_ROOT / "data" / "policy" / "Cold_Chain_Incident_SOP_v2.md").read_text(encoding="utf-8")
        (self.source / "Cold_Chain_Incident_SOP_v2.md").write_text(real_sop, encoding="utf-8")
        (self.source / "alarm_matrix.png").write_bytes(b"\x89PNG_REEFER_ALARM")
        self.run_ingest()

        service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(self.index, "sop"))
        sop_tool = make_inprocess_sop_tool(service, top_k=3)

        md_answer = sop_tool.invoke({"query": "Fresh Perishables 0.0°C and 4.0°C critical breach"})
        self.assertIn("COMPLIANCE SOP CONTEXT", md_answer)
        self.assertIn("Cold_Chain_Incident_SOP_v2.md", md_answer)

        img_answer = sop_tool.invoke({"query": "Code E-42 Auxiliary Compressor Fault"})
        self.assertIn("alarm_matrix.png", img_answer)
        self.assertIn("modality=image", img_answer)

        # Rejected empty or oversized query -> QUERY_REJECTED_MESSAGE
        self.assertEqual(sop_tool.invoke({"query": "   "}), QUERY_REJECTED_MESSAGE)
        self.assertEqual(sop_tool.invoke({"query": "x" * 2500}), QUERY_REJECTED_MESSAGE)

        # Backend failure -> UNAVAILABLE_MESSAGE
        self.index.fail_queries = True
        self.assertEqual(sop_tool.invoke({"query": "temperature"}), UNAVAILABLE_MESSAGE)

        # Empty match -> NO_MATCH_MESSAGE
        self.index.fail_queries = False
        empty_service = RetrievalService(
            Embedder(self.backend, "fake-model", dimension=DIM),
            VectorStore(FakeIndex(), "sop"),
        )
        empty_tool = make_inprocess_sop_tool(empty_service)
        self.assertEqual(empty_tool.invoke({"query": "temperature"}), NO_MATCH_MESSAGE)

    def test_orchestrator_domino_effect_and_restraint_with_audit_log(self):
        (self.source / "Cold_Chain_Incident_SOP_v2.md").write_text(SOP_MD, encoding="utf-8")
        self.run_ingest()

        fake_engine = FakeSqlEngine(
            columns=["Timestamp", "Latitude", "Longitude", "Current_Temperature_C", "Risk_Classification", "Delay_Probability"],
            rows=[("2023-07-13 22:00", 33.79, -118.12, 6.2, "High Risk", 0.812)],
        )
        sql_tool = make_telemetry_sql_tool(AgentSettings(_env_file=None), engine=fake_engine)
        weather_tool = make_corridor_conditions_tool(
            AgentSettings(_env_file=None),
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"current_weather": {"temperature": 26.0, "windspeed": 15.0}})
            ),
        )
        rag_service = RetrievalService(Embedder(self.backend, "fake-model", dimension=DIM), VectorStore(self.index, "sop"))
        sop_tool = make_inprocess_sop_tool(rag_service)
        audit_logger = AuditLogger(engine=fake_engine, enabled=True)

        # 1. "Domino Effect" multi-tool orchestration turn
        domino_script = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_sql_1",
                        "name": "query_telemetry_db",
                        "args": {"sql_query": "SELECT TOP 10 * FROM FDE_VIEWS.VW_ACTIVE_FLEET"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_weather_2",
                        "name": "fetch_corridor_conditions",
                        "args": {"latitude": 33.79, "longitude": -118.12},
                    },
                    {
                        "id": "call_sop_3",
                        "name": "search_compliance_sop",
                        "args": {"query": "Fresh perishables temperature breach above 4.0C mitigation"},
                    },
                ],
            ),
            AIMessage(
                content=(
                    "Here's what I found for active shipments and the weather check:\n\n"
                    "Cold-chain breach detected at **6.2°C 🔴** (> 4.0°C threshold per `sop.md`). "
                    "Divert to the Inland Empire Overflow Depot."
                )
            ),
        ]
        orchestrator = AgentOrchestrator(
            AgentSettings(_env_file=None),
            llm=ScriptedToolCallingLLM(domino_script),
            tools=[sql_tool, weather_tool, sop_tool],
            audit_logger=audit_logger,
        )
        answer = orchestrator.ask(
            "Find active shipments near Los Angeles, check weather, and verify SOP compliance.",
            session_id="domino-test-session",
        )
        self.assertIn("6.2°C", answer)
        self.assertIn("Inland Empire Overflow Depot", answer)

        # Verify all 3 tools executed and audit rows were persisted to FDE_VIEWS.AgentAuditLog
        recorded_tools = [e.tool_name for e in audit_logger.entries]
        self.assertIn("query_telemetry_db", recorded_tools)
        self.assertIn("fetch_corridor_conditions", recorded_tools)
        self.assertIn("search_compliance_sop", recorded_tools)
        self.assertIn("FinalSynthesis", recorded_tools)
        self.assertEqual(len(fake_engine.audit_rows), len(audit_logger.entries))

        # 2. "The Restraint" test (no tool invocation when answering conceptual question)
        restraint_audit = AuditLogger(engine=fake_engine, enabled=True)
        restraint_orchestrator = AgentOrchestrator(
            AgentSettings(_env_file=None),
            llm=ScriptedToolCallingLLM(
                [AIMessage(content="Tier 1 handles standard alerts; Tier 2 handles High Risk + Delay > 0.65.")]
            ),
            tools=[sql_tool, weather_tool, sop_tool],
            audit_logger=restraint_audit,
        )
        restraint_answer = restraint_orchestrator.ask(
            "Explain the difference between a Tier 1 and Tier 2 escalation.",
            session_id="restraint-session",
        )
        self.assertIn("Tier 1", restraint_answer)
        self.assertEqual(
            [e.node_executed for e in restraint_audit.entries],
            ["user_input", "reasoner"],
        )

    def test_agent_cli_dry_run_and_invalid_config_exit_codes(self):
        self.assertEqual(agent_cli.main(["--dry-run"]), 0)
        self.assertEqual(agent_cli.main(["--dry-run", "--llm", "INVALID_PROVIDER"]), 2)

    def test_prototype_src_and_legacy_sop_script_remain_untouched(self):
        tracked_prototype_files = [
            "src/agent_tools.py",
            "src/orchestrator.py",
            "src/ui.py",
            "src/prompts/system_prompt.txt",
            "scripts/ingest_sop_pinecone.py",
        ]
        result = subprocess.run(
            ["git", "diff", "--name-only", "--", *tracked_prototype_files],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Tracked prototype files were modified: {result.stdout}",
        )


if __name__ == "__main__":
    unittest.main()
