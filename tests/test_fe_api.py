import json
import unittest

from api.server import generate_dispatch_sse_frames

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


class TestPureSseAndAuditEngine(unittest.TestCase):
    """Offline stdlib-only tests for the FE/ BFF SSE stream & audit log writer."""

    def test_sse_generator_emits_ordered_frames_and_writes_audit_trail(self):
        audit_records = []

        def fake_streamer(session_id: str, prompt: str):
            yield {
                "type": "tool_input",
                "node": "reasoner",
                "name": "query_telemetry_db",
                "args": {"sql_query": "SELECT TOP 1 IOT_TEMP_VAL_C FROM FDE_VIEWS.VW_ACTIVE_FLEET"},
            }
            yield {
                "type": "tool_output",
                "node": "tools",
                "name": "query_telemetry_db",
                "content": "COLUMNS: IOT_TEMP_VAL_C\n(6.8,)",
            }
            yield {
                "type": "final_answer",
                "node": "reasoner_final",
                "name": "LLM Text Synthesis",
                "content": "Critical excursion detected at 6.8C (> 4.0C SOP threshold).",
            }

        def fake_writer(session_id: str, node_name: str, tool_name: str, content: str):
            audit_records.append((session_id, node_name, tool_name, content))

        frames = list(
            generate_dispatch_sse_frames("sess-42", "Check excursions", fake_streamer, fake_writer)
        )
        events = []
        for frame in frames:
            lines = frame.strip().split("\n")
            evt_type = lines[0].replace("event:", "").strip()
            evt_data = json.loads(lines[1].replace("data:", "").strip())
            events.append((evt_type, evt_data))

        self.assertEqual(
            [e[0] for e in events],
            ["status", "tool_input", "tool_output", "final_answer", "done"],
        )
        self.assertEqual(len(audit_records), 3)
        self.assertEqual(audit_records[0][1], "reasoner")
        self.assertEqual(audit_records[1][1], "tools")
        self.assertEqual(audit_records[2][1], "reasoner_final")

    def test_sse_generator_emits_timeout_error_when_no_final_answer(self):
        frames = list(
            generate_dispatch_sse_frames("sess-empty", "Ping", lambda s, p: iter([]), lambda *a: None)
        )
        event_types = [f.strip().split("\n")[0].replace("event:", "").strip() for f in frames]
        self.assertEqual(event_types, ["status", "error", "done"])


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestFeBffApi(unittest.TestCase):
    def setUp(self):
        from api.server import create_app

        self.audit_records = []

        def fake_streamer(session_id: str, prompt: str):
            yield {
                "type": "tool_input",
                "node": "reasoner",
                "name": "query_telemetry_db",
                "args": {"sql_query": "SELECT TOP 1 IOT_TEMP_VAL_C FROM FDE_VIEWS.VW_ACTIVE_FLEET"},
            }
            yield {
                "type": "tool_output",
                "node": "tools",
                "name": "query_telemetry_db",
                "content": "COLUMNS: IOT_TEMP_VAL_C\n(6.8,)",
            }
            yield {
                "type": "final_answer",
                "node": "reasoner_final",
                "name": "LLM Text Synthesis",
                "content": "Critical excursion detected at 6.8C (> 4.0C SOP threshold).",
            }

        def fake_writer(session_id: str, node_name: str, tool_name: str, content: str):
            self.audit_records.append(
                {
                    "LogID": len(self.audit_records) + 1,
                    "Timestamp": "2026-11-14T23:15:00",
                    "SessionID": session_id,
                    "NodeExecuted": node_name,
                    "ToolName": tool_name,
                    "Content": content,
                }
            )

        def fake_reader(username: str, password: str, session_id_filter=None, node_filter=None, limit=200):
            if username != "admin" or password != "secret":
                raise PermissionError("Invalid Administrator Credentials.")
            rows = list(self.audit_records)
            if session_id_filter:
                rows = [r for r in rows if r["SessionID"] == session_id_filter]
            if node_filter and node_filter != "ALL":
                rows = [r for r in rows if r["NodeExecuted"] == node_filter]
            return rows[:limit]

        self.app = create_app(
            agent_streamer=fake_streamer,
            audit_writer=fake_writer,
            audit_reader=fake_reader,
        )
        self.client = TestClient(self.app)

    def test_meta_endpoint(self):
        res = self.client.get("/api/v1/meta")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "fde-dispatch-bff")
        self.assertIn("agent_llm", body)
        self.assertIn("capabilities", body)
        self.assertIn("forecast_days", body["capabilities"]["weather_corridor"])

    def test_healthz_endpoint(self):
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    def test_session_purge_rotates_uuid(self):
        res = self.client.post("/api/v1/session/purge", json={"old_session_id": "old-123"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["purged"])
        self.assertEqual(body["previous_session_id"], "old-123")
        self.assertNotEqual(body["session_id"], "old-123")

    def test_dispatch_stream_emits_sse_and_writes_audit_logs(self):
        res = self.client.post(
            "/api/v1/dispatch/stream",
            json={"session_id": "sess-001", "prompt": "Check excursions > 4.0C"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(len(self.audit_records), 3)

    def test_audit_logs_auth_and_filtering(self):
        self.client.post(
            "/api/v1/dispatch/stream",
            json={"session_id": "sess-777", "prompt": "Check fleet"},
        )
        bad_res = self.client.post(
            "/api/v1/audit/logs",
            json={"username": "admin", "password": "wrong"},
        )
        self.assertEqual(bad_res.status_code, 401)

        ok_res = self.client.post(
            "/api/v1/audit/logs",
            json={
                "username": "admin",
                "password": "secret",
                "session_id_filter": "sess-777",
                "node_filter": "tools",
            },
        )
        self.assertEqual(ok_res.status_code, 200)
        payload = ok_res.json()
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rows"][0]["ToolName"], "query_telemetry_db")

    def test_langfuse_live_traces_and_observability_meta(self):
        from agent.tracing import get_langfuse_bridge

        get_langfuse_bridge().clear()
        stream_res = self.client.post(
            "/api/v1/dispatch/stream",
            json={"session_id": "sess-langfuse-99", "prompt": "Check excursions > 4.0C"},
        )
        self.assertEqual(stream_res.status_code, 200)
        self.assertIn("trace_id", stream_res.text)
        self.assertIn("span_id", stream_res.text)

        meta = self.client.get("/api/v1/meta").json()
        self.assertIn("observability", meta)
        self.assertEqual(meta["observability"]["sql_audit_table"], "FDE_VIEWS.AgentAuditLog")
        self.assertIn("USR_FDE_RO", meta["observability"]["sql_rbac_role"])

        traces_res = self.client.get("/api/v1/traces", params={"session_id": "sess-langfuse-99"})
        self.assertEqual(traces_res.status_code, 200)
        traces_body = traces_res.json()
        self.assertEqual(traces_body["count"], 3)
        first_span = traces_body["spans"][0]
        self.assertTrue(first_span["trace_id"].startswith("tr-"))
        self.assertTrue(first_span["span_id"].startswith("sp-"))


class TestObservabilitySubsystem(unittest.TestCase):
    """Tests for the dedicated BE/observability/ package (models, context, exporters, pipeline)."""

    def test_span_kind_classification_and_context_propagation(self):
        from observability import SpanKind, TraceContext, infer_span_kind

        self.assertEqual(infer_span_kind("reasoner", "LLM Text Synthesis"), SpanKind.GENERATION)
        self.assertEqual(infer_span_kind("tools", "query_telemetry_db"), SpanKind.TOOL)
        self.assertEqual(infer_span_kind("tools", "search_compliance_sop"), SpanKind.RETRIEVAL)
        self.assertEqual(infer_span_kind("user_input", "Dispatcher"), SpanKind.CHAIN)

        ctx = TraceContext.start("sess-obs-1")
        self.assertEqual(TraceContext.current(), ctx)
        self.assertTrue(ctx.trace_id.startswith("tr-"))

    def test_composite_pipeline_fault_isolation_and_filtering(self):
        from observability import ObservabilityPipeline

        pipeline = ObservabilityPipeline(max_spans=10)
        rec1 = pipeline.record_span(
            "sess-A",
            "reasoner",
            "query_telemetry_db",
            {"sql_query": "SELECT 1"},
            elapsed_ms=110,
            sql_persisted=True,
        )
        rec2 = pipeline.record_span(
            "sess-A",
            "tools",
            "search_compliance_sop",
            "SOP Section 3.2",
            elapsed_ms=240,
        )

        self.assertEqual(rec1.span_kind, "GENERATION")
        self.assertEqual(rec1.sink_status, "memory+sql")
        self.assertEqual(rec2.span_kind, "RETRIEVAL")

        tools_only = pipeline.query_spans(session_id_filter="sess-A", node_filter="tools")
        self.assertEqual(len(tools_only), 1)
        self.assertEqual(tools_only[0]["ToolName"], "search_compliance_sop")


if __name__ == "__main__":
    unittest.main()
