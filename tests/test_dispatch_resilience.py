"""Phase 6: one failure policy for data gathering and model steps (offline)."""
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from agent.config import AgentSettings
from agent.dispatch import resilience
from agent.dispatch.context import DispatchContext, DispatchResources
from agent.dispatch.nodes.finalize import render_final
from agent.dispatch.nodes.gather import gather_sop, gather_weather
from agent.dispatch.nodes.ledger import ledger
from agent.dispatch.nodes.planner import FALLBACK_GAP, planner
from agent.dispatch.nodes.reasoning import reasoner, route_after_reasoner
from agent.dispatch.resilience import EmptyModelReply, invoke_structured, is_transient_model_error, run_model_step
from agent.intent import classify_intent

try:  # `discover -s tests` puts tests/ on the path; `-m unittest tests.x` imports the package
    from test_dispatch_workflow import GOOD_DRAFT, NOW, FakeEngine, FakeRetrieval, make_graph, of_type, run
except ImportError:
    from tests.test_dispatch_workflow import GOOD_DRAFT, NOW, FakeEngine, FakeRetrieval, make_graph, of_type, run


class Status429(Exception):
    status_code = 429


class GoogleServerError(Exception):
    code = 503


class AuthenticationError(Exception):
    status_code = 401


class FailingLLM:
    """invoke() raises each given error in turn, then answers with `reply`."""

    def __init__(self, *errors, reply=None):
        self.errors, self.reply, self.calls = list(errors), reply, 0

    def invoke(self, messages):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.reply


def always(error):
    return FailingLLM(*[error] * 10)


def make_runtime(**resources):
    settings = AgentSettings(_env_file=None)
    events: list[dict] = []
    resources.setdefault("llm", object())
    ctx = DispatchContext(settings=settings, resources=DispatchResources(settings, **resources), clock=lambda: NOW)
    return Runtime(context=ctx, stream_writer=events.append), events


def setUpModule():
    patcher = mock.patch.object(resilience, "RETRY_WAIT_S", 0)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class TestRunModelStep(unittest.TestCase):
    def step(self, llm, **kw):
        events = []
        result = run_model_step("planner", lambda: llm.invoke([]), lambda: "fallback", emit=events.append,
                                fallback_name="standard scan", gap="planner: down", **kw)
        return result, events

    def test_transient_error_is_retried_once(self):
        llm = FailingLLM(Status429(), reply="ok")
        result, events = self.step(llm)
        self.assertEqual((result.value, result.degraded, result.gaps, llm.calls, events), ("ok", False, [], 2, []))

    def test_second_failure_falls_back(self):
        llm = FailingLLM(TimeoutError(), Status429(), reply="never")
        result, events = self.step(llm)
        self.assertEqual((result.value, result.degraded, result.gaps, llm.calls), ("fallback", True, ["planner: down"], 2))
        self.assertEqual(events, [{"type": "degraded", "step": "planner", "fallback": "standard scan", "reason": "Status429"}])

    def test_permanent_errors_fall_back_at_once_and_hide_the_message(self):
        for exc in (AuthenticationError("key sk-secret"), ValueError("bad"), EmptyModelReply()):
            llm = FailingLLM(exc, reply="never")
            result, events = self.step(llm)
            self.assertEqual((result.value, llm.calls), ("fallback", 1))
            self.assertNotIn("sk-secret", str(events) + str(result))

    def test_retry_can_be_turned_off(self):
        llm = FailingLLM(Status429(), reply="ok")
        self.assertTrue(self.step(llm, retry=False)[0].degraded)

    def test_transient_classification(self):
        for exc in (Status429(), GoogleServerError(), TimeoutError(), ConnectionError()):
            self.assertTrue(is_transient_model_error(exc), exc)
        for exc in (AuthenticationError(), ValueError(), EmptyModelReply()):
            self.assertFalse(is_transient_model_error(exc), exc)

    def test_missing_structured_output_is_an_error(self):
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.return_value = None
        with self.assertRaises(EmptyModelReply):
            invoke_structured(llm, dict, [])


class TestPlanner(unittest.TestCase):
    def plan(self, question, llm):
        runtime, events = make_runtime(planner_llm=llm)
        state = {"messages": [HumanMessage(question)], "question": question, "intent": classify_intent(question).to_state()}
        return planner(state, runtime), events

    def test_model_failure_uses_the_standard_scan(self):
        out, events = self.plan("How many shipments are above 4 °C near Long Beach?", always(AuthenticationError()))
        self.assertEqual([c["name"] for c in out["planned_calls"]], ["query_telemetry_db", "search_compliance_sop"])
        self.assertIn("Current_Temperature_C > 4", out["planned_calls"][0]["args"]["sql_query"])
        self.assertEqual(out["gaps"], [FALLBACK_GAP])
        self.assertEqual([e["type"] for e in events], ["degraded"])

    def test_without_area_or_filter_only_the_sop_is_searched(self):
        out, _ = self.plan("What is the average temperature per risk class?", always(ValueError()))
        self.assertEqual([c["name"] for c in out["planned_calls"]], ["search_compliance_sop"])

    def test_unknown_tool_becomes_a_gap(self):
        reply = AIMessage(content="", tool_calls=[
            {"name": "search_compliance_sop", "args": {"query": "q"}, "id": "1"},
            {"name": "delete_everything", "args": {}, "id": "2"},
        ])
        out, events = self.plan("What is the average temperature per risk class?", FailingLLM(reply=reply))
        self.assertEqual([c["name"] for c in out["planned_calls"]], ["search_compliance_sop"])
        self.assertEqual((out["gaps"], events), (["planner: unknown tool delete_everything"], []))


class TestReasoner(unittest.TestCase):
    def state(self):
        return {
            "messages": [HumanMessage("q")], "question": "q", "as_of": NOW.isoformat(), "attempts": 0, "gaps": [],
            "ledger": [{"id": "T1", "source": "telemetry", "origin": "view", "as_of": "2026-11-18T21:30:00+00:00",
                        "freshness": "fresh", "fields": {"Current_Temperature_C": 6.8}}],
            "triggers": [{"rule": "TEMP_BREACH", "evidence": "T1", "summary": "Current_Temperature_C 6.8 > 4.0",
                          "sop_ref": "SOP §1", "actions": ["restart_cooling"], "high_stakes": False}],
            "historical": [],
        }

    def test_failure_gives_an_evidence_only_answer_without_actions(self):
        runtime, events = make_runtime(llm=always(AuthenticationError()))
        out = reasoner(self.state(), runtime)
        self.assertIn("TEMP_BREACH on [T1]", out["draft"])
        self.assertNotIn("ACTION[", out["draft"])
        self.assertEqual((out["verification"]["passed"], out["verification"]["proposed_actions"]), (False, []))
        self.assertEqual([e["type"] for e in events], ["draft_start", "degraded", "draft_start", "verification"])
        self.assertEqual(route_after_reasoner(out), "action_gate")

    def test_transient_retry_restarts_the_streamed_draft(self):
        runtime, events = make_runtime(llm=FailingLLM(Status429(), reply=AIMessage("draft [T1]")))
        out = reasoner(self.state(), runtime)
        self.assertEqual(out, {"draft": "draft [T1]", "attempts": 1})
        self.assertEqual([e["type"] for e in events], ["draft_start", "draft_start"])
        self.assertEqual(route_after_reasoner(out), "verifier")

    def test_empty_reply_falls_back(self):
        runtime, _ = make_runtime(llm=FailingLLM(reply=AIMessage("")))
        self.assertTrue(reasoner(self.state(), runtime)["answer_degraded"])

    def test_a_client_that_cannot_be_built_is_an_error_not_a_fallback(self):
        runtime, events = make_runtime()
        with mock.patch.object(DispatchResources, "llm", side_effect=ValueError("GEMINI_API_KEY is required")):
            with self.assertRaises(ValueError):
                reasoner(self.state(), runtime)
        self.assertEqual(events, [])

    def test_footer_says_why_it_is_unverified_and_what_is_missing(self):
        text = render_final({"draft": "body", "gaps": ["answer: model unavailable"], "verification": {"degraded": True, "passed": False}})
        self.assertIn("**Unverified:** the language model was unavailable", text)
        self.assertIn("**Not available:** answer: model unavailable.", text)


class TestGatherAndLedger(unittest.TestCase):
    def test_weather_without_valid_coordinates_is_a_gap_and_never_fetched(self):
        client = mock.Mock()
        runtime, _ = make_runtime(http_client=client)
        for payload in ({}, {"latitude": 33.7}, {"latitude": 95, "longitude": 10}, {"latitude": "x", "longitude": 1},
                        {"latitude": float("nan"), "longitude": 1}):
            [item] = gather_weather(payload, runtime)["gathered"]
            self.assertEqual((item["status"], item["error"]), ("error", "no valid coordinates"))
        self.assertEqual(client.mock_calls, [])

    def test_source_outage_emits_degraded(self):
        runtime, events = make_runtime(retrieval_service=FakeRetrieval(fail=True))
        [item] = gather_sop({"query": "q"}, runtime)["gathered"]
        self.assertEqual(item["status"], "error")
        self.assertIn({"type": "degraded", "step": "sop", "fallback": "a data gap in the answer", "reason": "ConnectionError"}, events)

    def test_empty_results_are_notes_and_failures_are_gaps(self):
        runtime, _ = make_runtime()
        out = ledger({"gathered": [
            {"source": "sop", "status": "empty", "chunks": [], "error": None},
            {"source": "telemetry", "status": "empty", "columns": [], "rows": [], "error": None},
            {"source": "weather", "status": "error", "observations": None, "error": "no valid coordinates"},
        ]}, runtime)
        self.assertEqual(out["notes"], ["telemetry: no rows matched the query", "sop: no matching SOP section"])
        self.assertEqual(out["gaps"], ["weather: no valid coordinates"])


class TestWholeGraph(unittest.TestCase):
    def test_everything_failing_still_ends_with_an_answer_and_no_approval(self):
        graph, res, _ = make_graph(["unused"], engine=FakeEngine([], fail=True), retrieval=FakeRetrieval(fail=True),
                                   planner=always(AuthenticationError()))
        res._llm = always(AuthenticationError())
        with mock.patch("time.sleep"):  # telemetry has its own retry back-off
            events = run(graph, "What is the average temperature per risk class near Long Beach?")
        self.assertEqual(of_type(events, "approval_required"), [])
        [final] = of_type(events, "final_answer")
        self.assertFalse(final["verified"])
        for gap in (FALLBACK_GAP, "telemetry: telemetry database unavailable", "sop: SOP knowledge base unavailable", "answer: model unavailable"):
            self.assertIn(gap, final["content"])
        self.assertEqual({e["step"] for e in of_type(events, "degraded")}, {"planner", "telemetry", "sop", "reasoner"})

    def test_fresh_breach_with_the_reasoner_down_lists_it_but_never_asks_for_approval(self):
        graph, res, _ = make_graph([GOOD_DRAFT])
        res._llm = always(AuthenticationError())
        events = run(graph, "Which shipments are above 4 °C near Long Beach?")
        self.assertEqual(of_type(events, "approval_required"), [])
        self.assertIn("PORT_CONGESTION on [T1]", of_type(events, "final_answer")[0]["content"])

    def test_gaps_reset_between_turns(self):
        graph, res, _ = make_graph(["The SOP sets 7.0 [S1]."] * 4, retrieval=FakeRetrieval(fail=True))
        run(graph, "What does the SOP say about port congestion?")
        res._retrieval = FakeRetrieval()
        events = run(graph, "What does the SOP say about port congestion?")
        self.assertEqual(of_type(events, "evidence")[0]["gaps"], [])


class TestAudit(unittest.TestCase):
    def test_degraded_is_streamed_and_audited(self):
        from api.server import generate_dispatch_sse_frames

        rows = []
        steps = [{"type": "degraded", "step": "planner", "fallback": "standard scan", "reason": "AuthenticationError"},
                 {"type": "final_answer", "content": "x", "verified": False, "approval": None}]
        frames = list(generate_dispatch_sse_frames("s", "q", lambda s, p: iter(steps), lambda *a: rows.append(a)))
        self.assertIn(("s", "fallback", "DegradedStep"), [r[:3] for r in rows])
        self.assertTrue(any(f.startswith("event: degraded") for f in frames))


if __name__ == "__main__":
    unittest.main()
