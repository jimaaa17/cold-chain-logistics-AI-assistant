"""Unit tests for single dispatch nodes and routers, called directly with an injected Runtime
(no graph is built). Whole-graph behaviour is covered in test_dispatch_workflow.py."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from langgraph.runtime import Runtime
from langgraph.types import Send

from agent.config import AgentSettings
from agent.dispatch.context import DispatchContext, DispatchResources
from agent.dispatch.nodes.approval import action_gate
from agent.dispatch.nodes.intake import clarify, route_after_intake
from agent.dispatch.nodes.ledger import ledger, rules
from agent.dispatch.nodes.planner import route_after_planner
from agent.dispatch.nodes.reasoning import route_after_verifier
from agent.dispatch.streaming import iter_dispatch_events
from agent.intent import SCAN_SOP_QUERY, classify_intent
from agent.verifier import MAX_ATTEMPTS

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
COLUMNS = ["Timestamp", "Latitude", "Longitude", "Current_Temperature_C", "Cargo_Condition_Code",
           "Risk_Classification", "Delay_Probability", "Port_Congestion_Level", "Route_Risk_Index"]
FRESH_BREACH = ["2026-11-18T21:30:00", 33.77, -118.19, 6.8, 0.4, "High Risk", 0.81, 8.4, 0.7]
CALM = ["2026-11-18T21:00:00", 33.70, -118.20, 2.1, 0.2, "Low Risk", 0.10, 3.0, 0.1]
SOP_CHUNK = {"source_file": "Cold_Chain_Incident_SOP_v2.md", "text": "If PRT_CNG_LVL exceeds 7.0, divert.",
             "score": 0.6, "metadata": {"section": "2. Route Congestion"}}


def make_runtime(**overrides):
    settings = AgentSettings(_env_file=None)
    events: list[dict] = []
    ctx = DispatchContext(settings=settings, resources=DispatchResources(settings), clock=lambda: NOW, **overrides)
    return Runtime(context=ctx, stream_writer=events.append), events


def state_for(question):
    return {"question": question, "intent": classify_intent(question).to_state()}


class TestIntakeRouting(unittest.TestCase):
    def test_scan_with_area_fans_out_to_all_three_sources(self):
        runtime, _ = make_runtime()
        sends = route_after_intake(state_for("Which shipments are above 4 °C near Long Beach?"), runtime)
        self.assertEqual([s.node for s in sends], ["gather_telemetry", "gather_weather", "gather_sop"])
        self.assertIn("Current_Temperature_C > 4", sends[0].arg["sql"])
        self.assertEqual(sends[2].arg, {"query": SCAN_SOP_QUERY})

    def test_scan_sql_uses_settings_from_the_context(self):
        settings = AgentSettings(_env_file=None, sql_max_rows=3)
        ctx = DispatchContext(settings=settings, resources=DispatchResources(settings))
        sends = route_after_intake(state_for("Which shipments are above 4 °C near Long Beach?"), Runtime(context=ctx))
        self.assertIn("TOP 3", sends[0].arg["sql"])

    def test_sop_lookup_searches_the_sop_with_the_question(self):
        runtime, _ = make_runtime()
        question = "What does the SOP say about port congestion?"
        self.assertEqual(route_after_intake(state_for(question), runtime), [Send("gather_sop", {"query": question})])

    def test_open_and_clarify_routes(self):
        runtime, _ = make_runtime()
        self.assertEqual(route_after_intake(state_for("What is the average temperature per risk class?"), runtime), "planner")
        self.assertEqual(route_after_intake(state_for("Tell me a joke"), runtime), "clarify")

    def test_clarify_answers_with_the_context_text(self):
        runtime, _ = make_runtime(clarify_text="ask me about shipments")
        out = clarify({}, runtime)
        self.assertEqual(out["draft"], "ask me about shipments")
        self.assertTrue(out["verification"]["skipped"])


class TestPlannerRouting(unittest.TestCase):
    def test_tool_calls_map_to_gather_nodes_and_unknown_tools_are_dropped(self):
        state = {"planned_calls": [
            {"name": "query_telemetry_db", "args": {"sql_query": "SELECT 1"}},
            {"name": "fetch_corridor_conditions", "args": {"latitude": 1.0, "longitude": 2.0}},
            {"name": "search_compliance_sop", "args": {"query": "congestion"}},
            {"name": "delete_everything", "args": {}},
        ]}
        self.assertEqual(route_after_planner(state), [
            Send("gather_telemetry", {"sql": "SELECT 1"}),
            Send("gather_weather", {"latitude": 1.0, "longitude": 2.0}),
            Send("gather_sop", {"query": "congestion"}),
        ])

    def test_no_tool_calls_goes_straight_to_the_ledger(self):
        self.assertEqual(route_after_planner({"planned_calls": []}), "ledger")


class TestLedgerAndRules(unittest.TestCase):
    def test_records_are_ordered_by_source_and_numbered_per_source(self):
        runtime, _ = make_runtime()
        gathered = [
            {"source": "sop", "chunks": [SOP_CHUNK], "error": None},
            {"source": "telemetry", "columns": COLUMNS, "rows": [FRESH_BREACH], "error": None},
            {"source": "weather", "observations": None, "error": "weather service unavailable (ConnectError)"},
            {"source": "telemetry", "columns": COLUMNS, "rows": [CALM], "error": None},
        ]
        out = ledger({"gathered": gathered}, runtime)
        self.assertEqual([r["id"] for r in out["ledger"]], ["T1", "T2", "S1"])
        self.assertEqual(out["gaps"], ["weather: weather service unavailable (ConnectError)"])
        self.assertEqual(out["as_of"], NOW.isoformat())  # the injected clock, not the wall clock
        self.assertEqual(out["ledger"][0]["freshness"], "fresh")

    def test_rules_emits_the_evidence_summary(self):
        runtime, events = make_runtime()
        records = ledger({"gathered": [{"source": "telemetry", "columns": COLUMNS, "rows": [FRESH_BREACH], "error": None}]}, runtime)
        out = rules({**records, "gaps": ["sop: SOP knowledge base unavailable"]}, runtime)
        self.assertEqual({t["rule"] for t in out["triggers"]}, {"TEMP_BREACH", "PORT_CONGESTION", "HIGH_RISK_DELAY"})
        [event] = events
        self.assertEqual(event["type"], "evidence")
        self.assertEqual((event["records"], event["fresh"], event["historical_breaches"]), (1, 1, 0))
        self.assertIn("PORT_CONGESTION [T1]", event["triggers"])
        self.assertEqual(event["gaps"], ["sop: SOP knowledge base unavailable"])


class TestVerifierRouting(unittest.TestCase):
    def test_failed_draft_retries_until_the_attempt_limit(self):
        failed = {"verification": {"passed": False}}
        self.assertEqual(route_after_verifier({**failed, "attempts": MAX_ATTEMPTS - 1}), "reasoner")
        self.assertEqual(route_after_verifier({**failed, "attempts": MAX_ATTEMPTS}), "action_gate")
        self.assertEqual(route_after_verifier({"verification": {"passed": True}, "attempts": 1}), "action_gate")


class TestActionGate(unittest.TestCase):
    def test_no_high_stakes_action_does_not_interrupt(self):
        runtime, _ = make_runtime()
        state = {"verification": {"passed": True, "proposed_actions": [{"action": "restart_cooling", "evidence": "T1", "high_stakes": False}]}}
        self.assertEqual(action_gate(state, runtime), {"approval": None})

    def test_decision_is_stamped_with_the_injected_clock(self):
        runtime, _ = make_runtime()
        action = {"action": "divert_inland_empire", "evidence": "T1", "high_stakes": True}
        state = {
            "verification": {"passed": True, "proposed_actions": [action]},
            "triggers": [{"evidence": "T1", "actions": ["divert_inland_empire"], "summary": "congestion 8.4 > 7.0", "sop_ref": "SOP §2"}],
        }
        with mock.patch("agent.dispatch.nodes.approval.interrupt", return_value={"decision": "approve", "by": "dana"}) as intr:
            out = action_gate(state, runtime)
        request = intr.call_args.args[0]
        self.assertEqual(request["actions"][0]["trigger"], "congestion 8.4 > 7.0 (SOP §2)")
        self.assertEqual(out["approval"]["decision"], "approved")
        self.assertEqual(out["approval"]["at"], NOW.isoformat())


class TestContextWiring(unittest.TestCase):
    def test_iter_dispatch_events_passes_the_graph_context(self):
        sentinel = object()
        graph = SimpleNamespace(dispatch_context=sentinel, stream=mock.Mock(return_value=iter([])))
        list(iter_dispatch_events(graph, session_id="s", prompt="hi"))
        list(iter_dispatch_events(graph, session_id="s", resume={"decision": "approve"}))
        self.assertEqual([c.kwargs["context"] for c in graph.stream.call_args_list], [sentinel, sentinel])

    def test_built_graph_carries_a_context_with_its_settings_resources_and_clock(self):
        from agent.dispatch import build_dispatch_workflow

        settings = AgentSettings(_env_file=None)
        res = DispatchResources(settings)
        graph = build_dispatch_workflow(settings, resources=res, clock=lambda: NOW)
        ctx = graph.dispatch_context
        self.assertIs(ctx.settings, settings)
        self.assertIs(ctx.resources, res)
        self.assertIs(graph.dispatch_resources, res)
        self.assertEqual(ctx.clock(), NOW)
        self.assertIn("<evidence_ledger>", ctx.reasoner_prompt.messages[0].prompt.template)


class TestPublicSchema(unittest.TestCase):
    def test_invoke_returns_only_the_public_output_fields(self):
        from langchain_core.messages import HumanMessage

        from agent.dispatch import build_dispatch_workflow

        settings = AgentSettings(_env_file=None)
        graph = build_dispatch_workflow(settings, resources=DispatchResources(settings), clock=lambda: NOW)
        out = graph.invoke(  # clarify route: no tools, no LLM
            {"messages": [HumanMessage("Tell me a joke")]},
            config={"configurable": {"thread_id": "schema"}},
            context=graph.dispatch_context,
        )
        self.assertEqual(set(out), {"messages", "final", "verification", "approval"})
        self.assertIn("refrigerated shipments", out["final"])
        internal = graph.get_state({"configurable": {"thread_id": "schema"}}).values
        self.assertIn("ledger", internal)  # the checkpoint still holds the full internal state

    def test_input_schema_accepts_only_messages(self):
        from agent.dispatch import build_dispatch_workflow

        settings = AgentSettings(_env_file=None)
        graph = build_dispatch_workflow(settings, resources=DispatchResources(settings))
        self.assertEqual(set(graph.get_input_jsonschema()["properties"]), {"messages"})


if __name__ == "__main__":
    unittest.main()
