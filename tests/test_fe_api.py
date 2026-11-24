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
        self.assertEqual(len(audit_records), 4)
        self.assertEqual(audit_records[0], ("sess-42", "user_input", "Dispatcher", "Check excursions"))
        self.assertEqual(audit_records[1][1], "reasoner")
        self.assertEqual(audit_records[2][1], "tools")
        self.assertEqual(audit_records[3][1], "reasoner_final")

    def test_sse_generator_emits_timeout_error_when_no_final_answer(self):
        frames = list(
            generate_dispatch_sse_frames("sess-empty", "Ping", lambda s, p: iter([]), lambda *a: None)
        )
        event_types = [f.strip().split("\n")[0].replace("event:", "").strip() for f in frames]
        self.assertEqual(event_types, ["status", "error", "done"])


def _parse_frames(frames):
    events = []
    for frame in frames:
        lines = frame.strip().split("\n")
        events.append(
            (lines[0].replace("event:", "").strip(), json.loads(lines[1].replace("data:", "").strip()))
        )
    return events


class _FakeGraph:
    """Streams like the dispatch workflow (stream_mode list -> (mode, chunk)): one real tool call,
    then optionally raises mid-run."""

    dispatch_context = None  # iter_dispatch_events passes it to stream(); this fake ignores it

    def __init__(self, fail_mid_run=True, empty=False):
        self.fail_mid_run = fail_mid_run
        self.empty = empty

    def stream(self, *_args, **_kwargs):
        if self.empty:
            return
        yield ("custom", {"type": "tool_input", "name": "query_telemetry_db", "args": {"sql_query": "SELECT 1"}})
        if self.fail_mid_run:
            raise RuntimeError("upstream LLM 503")


class TestLivePathNeverFabricates(unittest.TestCase):
    """Regression: the canned demo stream must never be spliced into, or substituted for, a live run."""

    def setUp(self):
        from unittest import mock

        try:
            import langchain_core  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("langchain_core not installed")
        import api.server as server

        self.server = server
        self.mock = mock
        self.audit = []
        self.addCleanup(mock.patch.stopall)

    def _run(self, *, demo, graph=None, build_error=None):
        env = {"AGENT_OFFLINE_DEMO": "1"} if demo else {}
        self.mock.patch.dict("os.environ", env, clear=False).start()
        if not demo:
            import os

            os.environ.pop("AGENT_OFFLINE_DEMO", None)
        self.mock.patch("time.sleep").start()
        if build_error is not None:
            getter = self.mock.Mock(side_effect=self.server.AgentConfigError(build_error))
        else:
            getter = self.mock.Mock(return_value=graph)
        self.mock.patch.object(self.server, "_get_compiled_graph", getter).start()
        frames = generate_dispatch_sse_frames(
            "sess-x", "status?", self.server.default_agent_streamer, lambda *a: self.audit.append(a)
        )
        return _parse_frames(list(frames))

    def _assert_no_canned_data(self, events):
        blob = json.dumps(events) + json.dumps(self.audit)
        self.assertNotIn("6.8", blob)
        self.assertNotIn("Cold_Chain_Incident_SOP_v2.md", blob)
        self.assertNotIn(self.server.SIMULATED_AUDIT_PREFIX, blob)
        self.assertFalse(any(d.get("simulated") for _, d in events))

    def test_mid_run_failure_surfaces_error_without_demo(self):
        events = self._run(demo=False, graph=_FakeGraph())
        self.assertEqual([e for e, _ in events], ["status", "tool_input", "error", "done"])
        self.assertIn("upstream LLM 503", events[2][1]["message"])
        self._assert_no_canned_data(events)

    def test_mid_run_failure_is_not_padded_with_demo_even_when_demo_enabled(self):
        events = self._run(demo=True, graph=_FakeGraph())
        self.assertEqual([e for e, _ in events], ["status", "tool_input", "error", "done"])
        self._assert_no_canned_data(events)

    def test_empty_live_run_is_not_replaced_with_demo(self):
        events = self._run(demo=True, graph=_FakeGraph(empty=True))
        self.assertEqual([e for e, _ in events], ["status", "error", "done"])
        self._assert_no_canned_data(events)

    def test_build_failure_without_demo_reports_config_error(self):
        events = self._run(demo=False, build_error="Agent configuration error: missing key")
        self.assertEqual([e for e, _ in events], ["status", "error", "done"])
        self.assertIn("Agent configuration error", events[1][1]["message"])
        self._assert_no_canned_data(events)

    def test_demo_stream_is_marked_simulated_everywhere(self):
        events = self._run(demo=True, build_error="Agent configuration error: missing key")
        self.assertEqual(events[1][0], "status")
        self.assertEqual(events[1][1]["phase"], "simulated")
        content_events = [d for e, d in events if e in ("tool_input", "tool_output", "final_answer")]
        self.assertEqual(len(content_events), 5)
        self.assertTrue(all(d["simulated"] is True for d in content_events))
        self.assertTrue(events[-1][1]["simulated"])
        self.assertEqual(len(self.audit), 6)
        prompt_row, *demo_rows = self.audit
        self.assertEqual(prompt_row[1:], ("user_input", "Dispatcher", "status?"))  # the real prompt, unmarked
        self.assertTrue(all(row[3].startswith(self.server.SIMULATED_AUDIT_PREFIX) for row in demo_rows))


class TestCompiledGraphBuild(unittest.TestCase):
    """Regression: `_get_compiled_graph` build-error handling."""

    def setUp(self):
        from unittest import mock

        try:
            import agent.graph  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("agent dependencies not installed")
        import api.server as server

        self.server = server
        self.mock = mock
        self._saved_graph = server._ACTIVE_GRAPH
        server._ACTIVE_GRAPH = None
        self.addCleanup(setattr, server, "_ACTIVE_GRAPH", self._saved_graph)
        self.addCleanup(mock.patch.stopall)

    def _fail_build_with(self, exc):
        self.mock.patch("agent.workflow.build_dispatch_workflow", side_effect=exc).start()

    def test_build_error_message_never_contains_secrets(self):
        self.mock.patch.dict(
            "os.environ", {"GEMINI_API_KEY": "AIza-super-secret-123", "SQL_AGENT_PASSWORD": "Pa55word!"}
        ).start()
        self._fail_build_with(
            ValueError("auth failed for key AIza-super-secret-123 via UID=u;PWD=Pa55word!;Encrypt=no")
        )
        with self.assertRaises(self.server.AgentConfigError) as ctx:
            self.server._get_compiled_graph()
        msg = str(ctx.exception)
        self.assertIn("ValueError", msg)
        self.assertNotIn("AIza-super-secret-123", msg)
        self.assertNotIn("Pa55word!", msg)

    def test_build_error_message_is_length_capped(self):
        self._fail_build_with(RuntimeError("x" * 5000))
        with self.assertRaises(self.server.AgentConfigError) as ctx:
            self.server._get_compiled_graph()
        self.assertLess(len(str(ctx.exception)), 400)

    def test_build_failure_clears_settings_cache_and_caches_nothing(self):
        from agent.config import get_agent_settings

        get_agent_settings()
        self.assertEqual(get_agent_settings.cache_info().currsize, 1)
        self._fail_build_with(ValueError("GEMINI_API_KEY is required when AGENT_LLM=GEMINI."))
        with self.assertRaises(self.server.AgentConfigError):
            self.server._get_compiled_graph()
        self.assertEqual(get_agent_settings.cache_info().currsize, 0)
        self.assertIsNone(self.server._ACTIVE_GRAPH)

    def test_retry_after_fix_builds_once_and_is_cached(self):
        sentinel = object()
        build = self.mock.patch(
            "agent.workflow.build_dispatch_workflow", side_effect=[ValueError("missing key"), sentinel]
        ).start()
        with self.assertRaises(self.server.AgentConfigError):
            self.server._get_compiled_graph()
        self.assertIs(self.server._get_compiled_graph(), sentinel)
        self.assertIs(self.server._get_compiled_graph(), sentinel)
        self.assertEqual(build.call_count, 2)

    def test_concurrent_first_requests_build_only_once(self):
        import threading
        import time as _time

        calls = []

        def slow_build():
            calls.append(1)
            _time.sleep(0.05)
            return object()

        self.mock.patch("agent.workflow.build_dispatch_workflow", side_effect=slow_build).start()
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.server._get_compiled_graph()))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len({id(r) for r in results}), 1)

    def test_demo_fallback_is_refused_in_production(self):
        self.mock.patch.dict("os.environ", {"AGENT_OFFLINE_DEMO": "1", "APP_ENV": "production"}).start()
        self.mock.patch("time.sleep").start()
        self._fail_build_with(ValueError("missing key"))
        audit = []
        events = _parse_frames(
            list(
                generate_dispatch_sse_frames(
                    "sess-p", "status?", self.server.default_agent_streamer, lambda *a: audit.append(a)
                )
            )
        )
        self.assertEqual([e for e, _ in events], ["status", "error", "done"])
        self.assertIn("Agent configuration error", events[1][1]["message"])
        self.assertEqual(audit, [("sess-p", "user_input", "Dispatcher", "status?")])  # prompt only, no demo rows


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, statement, params=None):
        self.sink.append((str(statement), dict(params or {})))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self):
        self.statements = []

    def begin(self):
        return _FakeConn(self.statements)


class TestBffAuditPath(unittest.TestCase):
    """F4: the BFF audit writer/reader use AgentSettings, not raw os.getenv / DATABASE=master."""

    def setUp(self):
        from unittest import mock

        try:
            import agent.tools.telemetry_sql  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("agent dependencies not installed")
        import api.server as server
        from agent.config import get_agent_settings

        self.server, self.mock = server, mock
        mock.patch.dict(
            "os.environ",
            {
                "SQL_SERVER_HOST": "db.internal",
                "SQL_SERVER_DATABASE": "FleetDB",
                "SQL_AGENT_USER": "USR_FDE_RO",
                "SQL_AGENT_PASSWORD": "agent-pass-123",
                "AUDIT_TABLE": "FDE_VIEWS.AgentAuditLog",
                "SQL_ADMIN_USER": "fde_admin",
                "SQL_ADMIN_PASSWORD": "admin-pass-123",
            },
        ).start()
        get_agent_settings.cache_clear()
        mock.patch.object(server, "_AUDIT_EXPORTER", None).start()
        mock.patch.object(server, "_ADMIN_ENGINES", {}).start()
        self.engine_settings = []
        self.engine = _FakeEngine()

        def fake_create_engine(settings):
            self.engine_settings.append(settings)
            return self.engine

        mock.patch("agent.tools.telemetry_sql.create_telemetry_engine", side_effect=fake_create_engine).start()
        self.addCleanup(get_agent_settings.cache_clear)
        self.addCleanup(mock.patch.stopall)

    def test_writer_uses_configured_database_and_agent_role(self):
        self.server.default_audit_writer("sess-1", "user_input", "Dispatcher", "hello")
        self.server.default_audit_writer("sess-1", "tools", "query_telemetry_db", "rows")
        self.assertEqual(len(self.engine_settings), 1)  # engine built once, then reused
        conn_str = self.engine_settings[0].odbc_connection_string()
        self.assertIn("DATABASE={FleetDB}", conn_str)
        self.assertIn("SERVER=db.internal", conn_str)
        self.assertIn("UID={USR_FDE_RO}", conn_str)
        self.assertEqual(len(self.engine.statements), 2)
        sql, params = self.engine.statements[0]
        self.assertIn("INSERT INTO FDE_VIEWS.AgentAuditLog", sql)
        self.assertEqual(params["node_executed"], "user_input")

    def test_writer_truncates_to_column_sizes(self):
        self.server.default_audit_writer("s" * 80, "n" * 80, "t" * 150, "content")
        _, params = self.engine.statements[0]
        self.assertEqual((len(params["session_id"]), len(params["node_executed"]), len(params["tool_name"])), (50, 50, 100))

    def test_writer_never_raises_and_logs_when_settings_are_broken(self):
        self.mock.patch("agent.config.get_agent_settings", side_effect=ValueError("bad settings")).start()
        with self.assertLogs("api.server", level="WARNING") as logs:
            self.server.default_audit_writer("sess-1", "tools", "x", "y")
        self.assertIn("Audit ledger unavailable", logs.output[0])
        self.assertIsNone(self.server._AUDIT_EXPORTER)  # not cached; next write retries

    def test_writer_disabled_by_audit_enabled_false(self):
        self.mock.patch.dict("os.environ", {"AUDIT_ENABLED": "false"}).start()
        from agent.config import get_agent_settings

        get_agent_settings.cache_clear()
        self.server.default_audit_writer("sess-1", "tools", "x", "y")
        self.assertEqual(self.engine_settings, [])  # no engine, no INSERT
        self.assertEqual(self.engine.statements, [])

    def test_reader_uses_configured_database_with_admin_credentials(self):
        with self.assertLogs("api.server", level="WARNING") as logs:  # fake engine can't connect()
            self.server.default_audit_reader("fde_admin", "admin-pass-123")
        admin = self.engine_settings[0]
        self.assertEqual(admin.sql_agent_user, "fde_admin")
        self.assertIn("DATABASE={FleetDB}", admin.odbc_connection_string())
        self.assertIn("serving in-memory spans", logs.output[0])

    def test_reader_rejects_wrong_admin_credentials_before_touching_sql(self):
        with self.assertRaises(PermissionError):
            self.server.default_audit_reader("fde_admin", "wrong")
        self.assertEqual(self.engine_settings, [])


class TestMetaAndDemoDrift(unittest.TestCase):
    """F5: /meta comes from the real settings, both servers agree, demo SQL targets the view."""

    def setUp(self):
        from unittest import mock

        try:
            import agent.config  # noqa: F401
            import rag.config  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("settings dependencies not installed")
        import api.server as server
        from agent.config import get_agent_settings
        from rag.config import get_settings

        self.server, self.mock = server, mock
        mock.patch.dict("os.environ", {"SQL_AGENT_PASSWORD": "agent-pass-123"}).start()
        for cache in (get_agent_settings, get_settings):
            cache.cache_clear()
            self.addCleanup(cache.cache_clear)
        self.addCleanup(mock.patch.stopall)

    def test_meta_reports_the_llm_the_agent_actually_uses(self):
        # AGENT_LLM is the canonical name; the old /meta ignored it and never reported OPENAI.
        self.mock.patch.dict(
            "os.environ", {"AGENT_LLM": "openai", "OPENAI_API_KEY": "sk-test", "PINECONE_API_KEY": "pc-test"}
        ).start()
        meta = self.server.build_meta_payload()
        self.assertEqual(meta["agent_llm"], "OPENAI")
        self.assertEqual(meta["agent_model"], "gpt-4o")
        self.assertNotIn("config_errors", meta)

    def test_meta_reports_embeddings_provider_from_rag_settings(self):
        self.mock.patch.dict(
            "os.environ", {"EMBEDDINGS_PROVIDER": "OPENAI", "OPENAI_API_KEY": "sk-test", "PINECONE_API_KEY": "pc-test"}
        ).start()
        self.assertEqual(self.server.build_meta_payload()["embeddings_model"], "OPENAI")

    def test_meta_surfaces_config_errors_without_secrets(self):
        self.mock.patch.dict("os.environ", {"PINECONE_API_KEY": "pc-super-secret-999"}).start()
        self.mock.patch(
            "rag.config.get_settings", side_effect=ValueError("index auth failed for pc-super-secret-999")
        ).start()
        meta = self.server.build_meta_payload()
        self.assertEqual(meta["status"], "ok")  # the service is up; config problems are reported separately
        self.assertIsNone(meta["embeddings_model"])
        self.assertEqual(len(meta["config_errors"]), 1)
        self.assertTrue(meta["config_errors"][0].startswith("rag: "))
        self.assertNotIn("pc-super-secret-999", meta["config_errors"][0])

    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_stdlib_and_fastapi_meta_are_identical(self):
        import threading
        import urllib.request

        from api.server import create_app, make_stdlib_server

        fastapi_meta = TestClient(create_app(agent_streamer=lambda s, p: iter([]))).get("/api/v1/meta").json()
        server = make_stdlib_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/meta"
            with urllib.request.urlopen(url, timeout=5) as res:
                stdlib_meta = json.loads(res.read())
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(stdlib_meta, fastapi_meta)
        self.assertIn("capabilities", stdlib_meta)  # the stdlib copy used to omit it

    def test_demo_sql_targets_view_aliases_and_passes_the_guard(self):
        from agent.tools.telemetry_sql import validate_readonly_select

        self.mock.patch("time.sleep").start()
        events = list(self.server._offline_fallback_streamer("s", "p"))
        sql = events[0]["args"]["sql_query"]
        validate_readonly_select(sql)  # raises if the guard would reject it
        for legacy in ("TS_UTC", "V_LAT", "V_LON", "IOT_TEMP_VAL_C", "CGO_COND_CD", "PRT_CNG_LVL", "DELAY_PROB_DEC"):
            self.assertNotIn(legacy, sql)
            self.assertNotIn(legacy, events[1]["content"])
        self.assertIn("Current_Temperature_C", sql)


class TestSessionPurge(unittest.TestCase):
    """F6: /session/purge deletes the old thread's LangGraph checkpoint, not just mint a new id."""

    def setUp(self):
        from unittest import mock

        try:
            from langgraph.checkpoint.memory import MemorySaver  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("langgraph not installed")
        import api.server as server

        self.server, self.mock = server, mock
        mock.patch.object(server, "_ACTIVE_GRAPH", None).start()
        self.addCleanup(mock.patch.stopall)

    def _graph_with_memory(self, *session_ids):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, MessagesState, StateGraph

        builder = StateGraph(MessagesState)
        builder.add_node("reply", lambda state: {"messages": [AIMessage(content="ack")]})
        builder.add_edge(START, "reply")
        builder.add_edge("reply", END)
        graph = builder.compile(checkpointer=MemorySaver())
        for sid in session_ids:
            graph.invoke({"messages": [("user", f"hello from {sid}")]}, {"configurable": {"thread_id": sid}})
        return graph

    @staticmethod
    def _has_memory(graph, session_id):
        return graph.checkpointer.get_tuple({"configurable": {"thread_id": session_id}}) is not None

    def test_purge_deletes_only_the_old_sessions_memory(self):
        graph = self._graph_with_memory("old-123", "other-456")
        self.server._ACTIVE_GRAPH = graph
        self.assertTrue(self._has_memory(graph, "old-123"))

        body = self.server.build_purge_payload("old-123")

        self.assertTrue(body["purged"])
        self.assertEqual(body["previous_session_id"], "old-123")
        self.assertNotEqual(body["session_id"], "old-123")
        self.assertFalse(self._has_memory(graph, "old-123"))
        self.assertTrue(self._has_memory(graph, "other-456"))

    def test_purge_before_graph_is_built_is_a_no_op_success(self):
        self.assertTrue(self.server.build_purge_payload("never-used")["purged"])

    def test_purge_without_old_session_does_not_call_purger(self):
        purger = self.mock.Mock()
        body = self.server.build_purge_payload(None, purger)
        purger.assert_not_called()
        self.assertTrue(body["purged"])

    def test_failed_purge_reports_false_but_still_rotates_session(self):
        with self.assertLogs("api.server", level="WARNING"):
            body = self.server.build_purge_payload("old-1", self.mock.Mock(side_effect=RuntimeError("boom")))
        self.assertFalse(body["purged"])
        self.assertTrue(body["session_id"])

    def test_checkpointer_without_delete_reports_false(self):
        self.server._ACTIVE_GRAPH = self.mock.Mock(checkpointer=object())
        with self.assertLogs("api.server", level="WARNING"):
            self.assertFalse(self.server.build_purge_payload("old-1")["purged"])

    @unittest.skipIf(TestClient is None, "fastapi not installed")
    def test_endpoint_uses_injected_purger(self):
        from api.server import create_app

        calls = []
        client = TestClient(create_app(agent_streamer=lambda s, p: iter([]), session_purger=lambda sid: calls.append(sid) or True))
        body = client.post("/api/v1/session/purge", json={"old_session_id": "old-9"}).json()
        self.assertEqual(calls, ["old-9"])
        self.assertTrue(body["purged"])

    def test_stdlib_endpoint_purges_too(self):
        import threading
        import urllib.request

        graph = self._graph_with_memory("old-std")
        self.server._ACTIVE_GRAPH = graph
        server = self.server.make_stdlib_server("127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/v1/session/purge",
                data=json.dumps({"old_session_id": "old-std"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                body = json.loads(res.read())
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(body["purged"])
        self.assertFalse(self._has_memory(graph, "old-std"))


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
        self.assertEqual(len(self.audit_records), 4)
        self.assertEqual(self.audit_records[0]["NodeExecuted"], "user_input")
        self.assertEqual(self.audit_records[0]["Content"], "Check excursions > 4.0C")

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
        self.assertEqual(traces_body["count"], 4)  # prompt + tool_input + tool_output + final_answer
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
