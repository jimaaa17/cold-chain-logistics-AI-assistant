"""Offline tests for the agentic dispatch workflow (agent.workflow and its code-driven parts)."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import httpx
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agent.config import AgentSettings
from agent.evidence import FRESH, HISTORICAL, classify_freshness, telemetry_records, weather_records
from agent.intent import build_scan_sql, classify_intent
from agent.rules import evaluate_rules
from agent.verifier import verify_draft
from agent.workflow import DispatchResources, build_dispatch_workflow, iter_dispatch_events, pending_approval

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
COLUMNS = [
    "Timestamp", "Latitude", "Longitude", "Current_Temperature_C", "Cargo_Condition_Code",
    "Risk_Classification", "Delay_Probability", "Port_Congestion_Level", "Route_Risk_Index",
]
FRESH_BREACH = (datetime(2026, 11, 18, 21, 30), 33.77, -118.19, 6.8, 0.4, "High Risk", 0.81, 8.4, 0.7)
HISTORICAL_BREACH = (datetime(2023, 2, 14, 22, 0), 33.80, -118.45, 19.0, 0.9, "High Risk", 1.0, 9.5, 0.2)
CALM = (datetime(2026, 11, 18, 21, 0), 33.70, -118.20, 2.1, 0.2, "Low Risk", 0.10, 3.0, 0.1)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, columns, rows):
        self._c, self._r = columns, rows

    def keys(self):
        return self._c

    def fetchmany(self, n):
        return self._r[:n]


class FakeEngine:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.sql = rows, fail, []

    def connect(self):
        engine = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, clause, params=None):
                if engine.fail:
                    raise ConnectionError("db down")
                engine.sql.append(str(clause))
                return _Result(COLUMNS, engine.rows)

        return _Conn()


class FakeRetrieval:
    def __init__(self, fail=False):
        self.fail, self.queries, self.warmed = fail, [], 0

    def retrieve(self, query, top_k=5):
        if self.fail:
            raise ConnectionError("pinecone down")
        self.queries.append(query)
        return [
            SimpleNamespace(
                source_file="Cold_Chain_Incident_SOP_v2.md",
                text="If PRT_CNG_LVL exceeds 7.0, divert all active shipments to the Inland Empire Overflow Depot.",
                score=0.61,
                metadata={"section": "2. Route Congestion & Diversion Tactics"},
            )
        ]

    def warm_up(self):
        self.warmed += 1


class ExplodingPlanner:
    def invoke(self, messages):
        raise AssertionError("planner LLM must not be called on the fixed fan-out route")


class ScriptedPlanner:
    def __init__(self, calls):
        self.calls, self.invocations = calls, 0

    def invoke(self, messages):
        self.invocations += 1
        return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(self.calls)])


def weather_client(wind=10.2):
    payload = {"current_weather": {"time": "2026-11-18T21:45", "temperature": 20.4, "windspeed": wind, "winddirection": 250}}
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))


def make_graph(drafts, rows=(FRESH_BREACH,), *, planner=None, retrieval=None, engine=None, wind=10.2):
    drafts_iter = iter([AIMessage(content=d) for d in drafts])
    llm = GenericFakeChatModel(messages=drafts_iter)
    settings = AgentSettings(_env_file=None)
    res = DispatchResources(
        settings,
        llm=llm,
        planner_llm=planner or ExplodingPlanner(),
        engine=engine or FakeEngine(list(rows)),
        http_client=weather_client(wind),
        retrieval_service=retrieval or FakeRetrieval(),
    )
    graph = build_dispatch_workflow(settings, resources=res, clock=lambda: NOW)
    return graph, res, drafts_iter


def run(graph, prompt=None, resume=None, session="s1"):
    return list(iter_dispatch_events(graph, session_id=session, prompt=prompt, resume=resume))


def of_type(events, kind):
    return [e for e in events if e["type"] == kind]


GOOD_DRAFT = (
    "## Summary\nReading [T1] is 6.8 °C with Port_Congestion_Level 8.4.\n\n"
    "## SOP assessment\n"
    "ACTION[restart_cooling] on [T1]: 6.8 > 4.0 per [S1].\n"
    "ACTION[divert_inland_empire] on [T1]: 8.4 > 7.0 per [S1].\n"
    "ACTION[escalate_tier2] on [T1]: delay 0.81 > 0.65.\n"
    "Weather [W1]: wind 10.2 km/h."
)


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------
class TestIntentRouting(unittest.TestCase):
    def test_standard_question_is_a_scan_with_area_and_threshold(self):
        intent = classify_intent("Which shipments are above 4 °C near Long Beach and what does the SOP require?")
        self.assertEqual(intent.kind, "incident_scan")
        self.assertEqual(intent.place, "long beach")
        self.assertEqual(intent.min_temp_c, 4.0)

    def test_longest_place_name_wins_and_la_abbreviation(self):
        self.assertEqual(classify_intent("Any delays at the Port of Long Beach?").place, "port of long beach")
        self.assertEqual(classify_intent("Show shipments near LA").place, "los angeles")

    def test_policy_question_is_sop_lookup(self):
        self.assertEqual(classify_intent("What does the SOP say about port congestion?").kind, "sop_lookup")

    def test_aggregate_question_goes_to_planner(self):
        self.assertEqual(classify_intent("What is the average temperature per risk class?").kind, "open")

    def test_off_topic_is_clarified(self):
        self.assertEqual(classify_intent("Tell me a joke").kind, "clarify")
        self.assertEqual(classify_intent("hi").kind, "clarify")

    def test_scan_sql_is_bounded_and_passes_the_guard(self):
        from agent.tools.telemetry_sql import validate_readonly_select

        sql = build_scan_sql(classify_intent("Shipments above 4 °C near Long Beach"), half_width_deg=0.35, max_rows=10)
        validate_readonly_select(sql)
        self.assertIn("Latitude BETWEEN 33.4200 AND 34.1200", sql)
        self.assertIn("Current_Temperature_C > 4", sql)
        self.assertIn("ORDER BY Timestamp DESC", sql)


# ---------------------------------------------------------------------------
# Evidence ledger & SOP rules
# ---------------------------------------------------------------------------
class TestEvidenceAndRules(unittest.TestCase):
    def records(self, *rows):
        return telemetry_records(COLUMNS, rows, now=NOW, window_hours=24)

    def test_freshness_window(self):
        self.assertEqual(classify_freshness(datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc), NOW, 24), FRESH)
        self.assertEqual(classify_freshness(datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc), NOW, 24), HISTORICAL)
        self.assertEqual(classify_freshness(None, NOW, 24), "undated")

    def test_fresh_breach_triggers_all_three_sop_rules(self):
        triggers, historical = evaluate_rules(self.records(FRESH_BREACH))
        self.assertEqual({t["rule"] for t in triggers}, {"TEMP_BREACH", "PORT_CONGESTION", "HIGH_RISK_DELAY"})
        self.assertEqual(historical, [])

    def test_temperature_breach_alone_never_permits_a_diversion(self):
        # SOP §1 diverts only if ETA delay > 1 h; telemetry has no ETA field, so code can't permit it.
        triggers, _ = evaluate_rules(self.records(FRESH_BREACH))
        temp = [t for t in triggers if t["rule"] == "TEMP_BREACH"][0]
        self.assertEqual(temp["actions"], ["restart_cooling"])
        self.assertFalse(temp["high_stakes"])

    def test_historical_breach_never_triggers_actions(self):
        triggers, historical = evaluate_rules(self.records(HISTORICAL_BREACH))
        self.assertEqual(triggers, [])
        self.assertIn("PORT_CONGESTION", {h["rule"] for h in historical})

    def test_port_rule_reads_only_the_telemetry_field_and_area(self):
        outside_ports = (datetime(2026, 11, 18, 21, 0), 34.11, -117.29, 2.0, 0.1, "Low Risk", 0.1, 9.0, 0.1)
        self.assertEqual(evaluate_rules(self.records(outside_ports))[0], [])

    def test_wind_never_triggers_a_rule(self):
        obs = {"latitude": 33.77, "longitude": -118.19, "current": {"time": "2026-11-18T21:45", "wind_kmh": 95.0}, "daily": []}
        ledger = weather_records(obs, now=NOW, window_hours=24) + self.records(CALM)
        self.assertEqual(evaluate_rules(ledger), ([], []))


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------
class TestVerifier(unittest.TestCase):
    def setUp(self):
        self.ledger = telemetry_records(COLUMNS, [FRESH_BREACH, HISTORICAL_BREACH], now=NOW, window_hours=24)
        self.ledger.append({"id": "S1", "source": "sop", "origin": "SOP §2", "as_of": None, "freshness": "undated", "fields": {}, "text": "exceeds 7.0"})
        self.triggers, self.historical = evaluate_rules(self.ledger)

    def check(self, draft):
        return verify_draft(draft, ledger=self.ledger, triggers=self.triggers, historical=self.historical,
                            question="Shipments above 4 °C?", window_hours=24)

    def test_grounded_draft_passes(self):
        result = self.check(
            "[T1] is at 6.8 °C (above 4.0); [T2] reached 19.0 °C on 2023-02-14 22:00 (historical).\n"
            "1. ACTION[divert_inland_empire] on [T1]: congestion 8.4 > 7.0 [S1]."
        )
        self.assertTrue(result["passed"], result["problems"])
        self.assertEqual(result["proposed_actions"], [{"action": "divert_inland_empire", "evidence": "T1", "high_stakes": True}])

    def test_invented_number_fails(self):
        result = self.check("[T1] is at 6.8 °C and congestion index is 8.5/10.")
        self.assertFalse(result["passed"])
        self.assertIn("8.5", result["problems"][0])

    def test_percent_of_a_ledger_value_is_grounded(self):
        self.assertTrue(self.check("[T1] has an 81% delay probability.")["passed"])

    def test_unknown_evidence_id_fails(self):
        self.assertFalse(self.check("[T1] and [T9] are breached.")["passed"])

    def test_action_on_historical_evidence_is_blocked(self):
        result = self.check("[T1] ok. ACTION[divert_inland_empire] on [T2]: 9.5 > 7.0.")
        self.assertFalse(result["passed"])
        self.assertTrue(any("not permitted" in p for p in result["problems"]))

    def test_calling_historical_readings_active_fails_unless_negated(self):
        ledger = telemetry_records(COLUMNS, [HISTORICAL_BREACH], now=NOW, window_hours=24)
        _, hist = evaluate_rules(ledger)
        kwargs = dict(ledger=ledger, triggers=[], historical=hist, question="", window_hours=24)
        self.assertFalse(verify_draft("These active shipments [T1] need attention.", **kwargs)["passed"])
        self.assertTrue(verify_draft("There are no active shipments; [T1] is historical.", **kwargs)["passed"])

    def test_timestamp_years_and_weather_point_are_grounded(self):
        # Live regression: both were rejected although they come from the ledger.
        obs = {"latitude": 33.77, "longitude": -118.19, "current": {"time": "2026-11-18T21:45", "wind_kmh": 13.6}, "daily": []}
        ledger = self.ledger + weather_records(obs, now=NOW, window_hours=24)
        result = verify_draft(
            "Weather at 33.77, -118.19 [W1]: wind 13.6 km/h. [T2] dates from 2023. [T1] is 6.8 °C.",
            ledger=ledger, triggers=[], historical=self.historical, question="", window_hours=24,
        )
        self.assertNotIn("Numbers not found", " ".join(result["problems"]))

    def test_unreported_fresh_trigger_fails(self):
        self.assertFalse(self.check("Everything is fine. [T2] is historical.")["passed"])


# ---------------------------------------------------------------------------
# Workflow graph
# ---------------------------------------------------------------------------
class TestDispatchWorkflow(unittest.TestCase):
    def test_scan_fans_out_without_planner_and_streams_tokens(self):
        graph, res, _ = make_graph([GOOD_DRAFT])
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        self.assertEqual(
            sorted(e["name"] for e in of_type(events, "tool_input")),
            ["fetch_corridor_conditions", "query_telemetry_db", "search_compliance_sop"],
        )  # ExplodingPlanner proves no planning call
        self.assertGreater(len(of_type(events, "token")), 5)
        self.assertTrue(of_type(events, "verification")[0]["passed"])
        self.assertIn("Current_Temperature_C > 4", res.engine().sql[0])

    def test_sop_lookup_only_searches_the_sop(self):
        graph, _, _ = make_graph(["The SOP sets 7.0 as the port congestion threshold [S1]."])
        events = run(graph, "What does the SOP say about port congestion?")
        self.assertEqual([e["name"] for e in of_type(events, "tool_input")], ["search_compliance_sop"])
        self.assertTrue(of_type(events, "final_answer")[0]["verified"])

    def test_clarify_uses_no_tools_and_no_llm(self):
        graph, _, drafts = make_graph(["unused"])
        events = run(graph, "Tell me a joke")
        self.assertEqual(of_type(events, "tool_input"), [])
        self.assertIn("refrigerated shipments", of_type(events, "final_answer")[0]["content"])
        self.assertIsNotNone(next(drafts, None))  # the reasoner never consumed its draft

    def test_open_question_uses_the_planner(self):
        planner = ScriptedPlanner([("query_telemetry_db", {"sql_query": "SELECT TOP 5 Timestamp, Current_Temperature_C FROM FDE_VIEWS.VW_ACTIVE_FLEET"})])
        graph, _, _ = make_graph(["Reading [T1] is 6.8 °C. ACTION[restart_cooling] on [T1]: above 4.0."], planner=planner)
        events = run(graph, "What is the average temperature per risk class?")
        self.assertEqual(planner.invocations, 1)
        self.assertEqual([e["name"] for e in of_type(events, "tool_input")], ["query_telemetry_db"])

    def test_one_retry_then_pass(self):
        graph, _, drafts = make_graph(["[T1] congestion index 8.5/10.", GOOD_DRAFT])
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        verdicts = of_type(events, "verification")
        self.assertEqual([v["passed"] for v in verdicts], [False, True])
        self.assertTrue(verdicts[0]["retrying"])
        self.assertEqual([d["attempt"] for d in of_type(events, "draft_start")], [1, 2])
        self.assertIsNone(next(drafts, None))

    def test_second_failure_is_marked_unverified_not_retried_again(self):
        graph, _, drafts = make_graph(["[T1] index 8.5.", "[T1] index 8.6.", GOOD_DRAFT], rows=(CALM,))
        events = run(graph, "Which shipments are near Long Beach?")
        self.assertEqual(len(of_type(events, "verification")), 2)
        final = of_type(events, "final_answer")[0]
        self.assertFalse(final["verified"])
        self.assertIn("**Unverified:**", final["content"])
        self.assertIsNotNone(next(drafts, None))  # third draft never requested

    def test_high_stakes_action_pauses_for_approval_then_approve(self):
        graph, _, _ = make_graph([GOOD_DRAFT])
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        request = of_type(events, "approval_required")[0]
        self.assertEqual([a["action"] for a in request["actions"]], ["divert_inland_empire"])
        self.assertIn("Port_Congestion_Level 8.4 > 7.0", request["actions"][0]["trigger"])
        self.assertEqual(of_type(events, "final_answer"), [])
        self.assertIsNotNone(pending_approval(graph, "s1"))

        resumed = run(graph, resume={"decision": "approve", "by": "dana", "note": "depot confirmed"})
        final = of_type(resumed, "final_answer")[0]
        self.assertEqual(final["approval"]["decision"], "approved")
        self.assertIn("approved by dana", final["content"])
        self.assertIsNone(pending_approval(graph, "s1"))

    def test_reject_is_recorded_and_tells_operator_not_to_execute(self):
        graph, _, _ = make_graph([GOOD_DRAFT])
        run(graph, "Which shipments are above 4 °C near Long Beach?")
        final = of_type(run(graph, resume={"decision": "reject", "by": "lee"}), "final_answer")[0]
        self.assertIn("must not be executed", final["content"])

    def test_historical_data_never_reaches_the_gate(self):
        draft = "[T1] reached 19.0 °C on 2023-02-14 22:00; it is historical. ACTION[divert_inland_empire] on [T1]: 9.5 > 7.0."
        graph, _, _ = make_graph([draft, draft], rows=(HISTORICAL_BREACH,))
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        self.assertEqual(of_type(events, "approval_required"), [])
        self.assertIn("**Blocked action**", of_type(events, "final_answer")[0]["content"])

    def test_source_outage_becomes_a_reported_gap(self):
        graph, _, _ = make_graph(["The SOP knowledge base is unavailable."], retrieval=FakeRetrieval(fail=True))
        events = run(graph, "What does the SOP say about port congestion?")
        self.assertIn("sop: SOP knowledge base unavailable", of_type(events, "evidence")[0]["gaps"])

    def test_warm_loads_every_resource_and_never_raises(self):
        retrieval = FakeRetrieval()
        res = DispatchResources(AgentSettings(_env_file=None), llm=object(), engine=object(),
                                http_client=object(), retrieval_service=retrieval)
        self.assertEqual(set(res.warm().values()), {"ready"})
        self.assertEqual(retrieval.warmed, 1)
        broken = DispatchResources(AgentSettings(_env_file=None), llm=object(), engine=object(), http_client=object(),
                                   retrieval_service=SimpleNamespace(warm_up=mock.Mock(side_effect=OSError("no model"))))
        self.assertEqual(broken.warm()["sop_retrieval"], "failed: OSError")


# ---------------------------------------------------------------------------
# BFF integration
# ---------------------------------------------------------------------------
try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


def _events(sse_text):
    out = []
    for frame in sse_text.strip().split("\n\n"):
        lines = frame.split("\n")
        out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestBffWorkflowIntegration(unittest.TestCase):
    def setUp(self):
        import api.server as server

        self.server = server
        self.graph, _, _ = make_graph([GOOD_DRAFT])
        patcher = mock.patch.object(server, "_ACTIVE_GRAPH", self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.audit = []
        self.client = TestClient(server.create_app(audit_writer=lambda *a: self.audit.append(a), prewarm=False))

    def test_stream_pauses_then_approve_endpoint_resumes_and_audits(self):
        res = self.client.post("/api/v1/dispatch/stream", json={"session_id": "bff-1", "prompt": "Which shipments are above 4 °C near Long Beach?"})
        events = _events(res.text)
        kinds = [k for k, _ in events]
        self.assertIn("token", kinds)
        self.assertIn("approval_required", kinds)
        self.assertEqual(events[-1][1]["outcome"], "awaiting_approval")
        audited_nodes = [row[1] for row in self.audit]
        self.assertIn("ledger", audited_nodes)
        self.assertIn("verifier", audited_nodes)
        self.assertIn("action_gate", audited_nodes)
        self.assertNotIn("token", audited_nodes)

        before = len(self.audit)
        res2 = self.client.post("/api/v1/dispatch/approve", json={"session_id": "bff-1", "decision": "approve", "by": "dana"})
        events2 = _events(res2.text)
        self.assertEqual(events2[-1][1]["outcome"], "complete")
        self.assertIn("approved by dana", [d for k, d in events2 if k == "final_answer"][0]["content"])
        resumed_rows = self.audit[before:]
        self.assertEqual(resumed_rows[0][1:3], ("approval", "Dispatcher"))  # the decision is audited first
        self.assertEqual([r[1] for r in resumed_rows[1:]], ["dispatcher", "reasoner_final"])

    def test_approve_without_pending_action_is_an_error_frame(self):
        events = _events(self.client.post("/api/v1/dispatch/approve", json={"session_id": "nothing-here", "decision": "approve"}).text)
        self.assertIn("No action is waiting for approval", [d for k, d in events if k == "error"][0]["message"])

    def test_invalid_decision_is_rejected(self):
        res = self.client.post("/api/v1/dispatch/approve", json={"session_id": "x", "decision": "maybe"})
        self.assertEqual(res.status_code, 422)

    def test_startup_prewarms_the_real_agent(self):
        with mock.patch.object(self.server, "prewarm_agent", return_value={"sop_retrieval": "ready"}) as warm:
            with TestClient(self.server.create_app(prewarm=True)) as client:
                client.get("/healthz")
        warm.assert_called_once()

    def test_injected_streamer_does_not_prewarm_by_default(self):
        with mock.patch.object(self.server, "prewarm_agent") as warm:
            with TestClient(self.server.create_app(agent_streamer=lambda s, p: iter([]))) as client:
                client.get("/healthz")
        warm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
