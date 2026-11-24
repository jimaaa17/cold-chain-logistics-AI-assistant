"""Phase 7: model routing, thresholds and verification inside the coded guardrails (offline)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.config import AgentSettings
from agent.dispatch import resilience
from agent.dispatch.context import DispatchResources
from agent.dispatch.nodes.assess import FALLBACK_GAP as ASSESS_GAP
from agent.dispatch.nodes.assess import check_finding
from agent.dispatch.nodes.reasoning import JUDGE_FALLBACK_GAP
from agent.dispatch.nodes.route import FALLBACK_GAP as ROUTE_GAP
from agent.dispatch.nodes.route import guard_route
from agent.dispatch.schemas import Assessment, Finding, RouteDecision, Verdict
from agent.dispatch.prompts import assessor_prompt, judge_prompt, router_prompt
from agent.intent import classify_intent
from agent.llm import LLMTask, build_task_llm
from agent.rules import evaluate_rules
from agent.workflow import build_dispatch_workflow

try:
    from test_dispatch_workflow import CALM, FRESH_BREACH, GOOD_DRAFT, NOW, FakeRetrieval, make_graph, of_type, run
except ImportError:
    from tests.test_dispatch_workflow import CALM, FRESH_BREACH, GOOD_DRAFT, NOW, FakeRetrieval, make_graph, of_type, run

SCAN = "Which shipments are above 4 °C near Long Beach?"
RISK_QUOTE = "RT_RSK_IDX above 0.6 requires a route review"


class FakeTaskLLM:
    """Structured-output model: returns (or raises) `reply` and records the prompts."""

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.prompts.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class AuthenticationError(Exception):
    status_code = 401


def graph_with(drafts, *, task_llms=None, rows=(FRESH_BREACH,), retrieval=None, planner=None, **modes):
    _, res, _ = make_graph(drafts, rows=rows, retrieval=retrieval, planner=planner)
    res.settings = AgentSettings(_env_file=None, **modes)
    res._task_llms = dict(task_llms or {})
    return build_dispatch_workflow(res.settings, resources=res, clock=lambda: NOW)


def decisions(events):
    return of_type(events, "model_decision")


def setUpModule():
    patcher = mock.patch.object(resilience, "RETRY_WAIT_S", 0)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class TestDefaults(unittest.TestCase):
    def test_every_mode_defaults_to_code(self):
        cfg = AgentSettings(_env_file=None)
        self.assertEqual({cfg.routing_mode, cfg.thresholds_mode, cfg.verification_mode}, {"code"})

    def test_code_mode_never_asks_a_model(self):
        llms = {t: FakeTaskLLM(AssertionError("must not be called")) for t in LLMTask}
        events = run(graph_with([GOOD_DRAFT], task_llms=llms), SCAN)
        self.assertEqual([llm.prompts for llm in llms.values()], [[], [], []])
        self.assertEqual(decisions(events), [])

    def test_prompt_variables(self):
        self.assertEqual(set(router_prompt.input_variables), {"question", "places"})
        self.assertEqual(set(assessor_prompt.input_variables), {"ledger", "field_names", "port_lat", "port_lon"})
        self.assertEqual(set(judge_prompt.input_variables), {"question", "ledger", "draft", "gaps"})

    def test_task_models_share_the_reasoner_unless_configured(self):
        llm = object()
        res = DispatchResources(AgentSettings(_env_file=None), llm=llm)
        self.assertIs(res.task_llm(LLMTask.JUDGE), llm)
        cfg = AgentSettings(_env_file=None, agent_llm="OPENAI", judge_model="gpt-x", llm_temperature=0.7)
        with mock.patch("agent.llm.build_reasoner_llm") as build:
            self.assertIsNone(build_task_llm(cfg, LLMTask.ROUTER))
            build_task_llm(cfg, LLMTask.JUDGE)
        used = build.call_args.args[0]
        self.assertEqual((used.openai_model, used.llm_temperature), ("gpt-x", 0.0))


class TestRouting(unittest.TestCase):
    QUESTION = "Is the cold corridor by the harbour okay this morning?"  # code: open

    def guard(self, question, **fields):
        return guard_route(RouteDecision(kind="incident_scan", reasoning="r", **fields), question, classify_intent(question))

    def test_gazetteer_replaces_model_coordinates(self):
        intent, gaps = self.guard(self.QUESTION, place="Long Beach", latitude=1.0, longitude=2.0)
        self.assertEqual((intent.place, intent.latitude, intent.longitude, gaps), ("long beach", 33.77, -118.19, []))

    def test_invented_coordinates_places_and_limits_are_dropped(self):
        intent, gaps = self.guard(self.QUESTION, place="atlantis", latitude=10.5, longitude=20.5, min_temp_c=4.0)
        self.assertFalse(intent.has_area)
        self.assertIsNone(intent.min_temp_c)
        self.assertIn("unknown place 'atlantis'", gaps[0])

    def test_values_in_the_question_are_kept_and_code_wins(self):
        intent, _ = self.guard("Reefers warmer than 6 at 33 -118", latitude=33, longitude=-118, min_temp_c=6)
        self.assertEqual((intent.latitude, intent.longitude, intent.min_temp_c), (33, -118, 6))
        intent, _ = self.guard("Average temperature near Long Beach", place="san diego")
        self.assertEqual(intent.place, "long beach")

    def test_model_mode_reroutes_an_unclear_question(self):
        router = FakeTaskLLM(RouteDecision(kind="incident_scan", reasoning="status question", place="long beach"))
        events = run(graph_with([GOOD_DRAFT], task_llms={LLMTask.ROUTER: router}, routing_mode="model"), self.QUESTION)
        self.assertEqual(len(of_type(events, "tool_input")), 3)  # fan-out; ExplodingPlanner never called
        [d] = decisions(events)
        self.assertEqual((d["decision"], d["mode"], d["agreed"], d["model"]["kind"]), ("routing", "model", False, "incident_scan"))

    def test_model_is_not_asked_when_code_is_confident(self):
        router = FakeTaskLLM(AssertionError("must not be called"))
        run(graph_with([GOOD_DRAFT], task_llms={LLMTask.ROUTER: router}, routing_mode="model"), SCAN)
        self.assertEqual(router.prompts, [])

    def test_shadow_keeps_the_code_route(self):
        router = FakeTaskLLM(RouteDecision(kind="incident_scan", reasoning="r"))
        events = run(graph_with([GOOD_DRAFT], task_llms={LLMTask.ROUTER: router}, routing_mode="shadow"), "Tell me a joke")
        self.assertEqual(of_type(events, "tool_input"), [])  # still clarify
        self.assertFalse(decisions(events)[0]["agreed"])

    def test_model_failure_falls_back_to_code_with_a_gap(self):
        events = run(graph_with([], task_llms={LLMTask.ROUTER: FakeTaskLLM(AuthenticationError())}, routing_mode="model"), "Tell me a joke")
        self.assertIn(ROUTE_GAP, of_type(events, "final_answer")[0]["content"])
        self.assertEqual(of_type(events, "degraded")[0]["step"], "routing")

    def test_shadow_failure_never_touches_the_answer(self):
        for graph in (
            graph_with([], task_llms={LLMTask.ROUTER: FakeTaskLLM(AuthenticationError())}, routing_mode="shadow"),
            graph_with([], routing_mode="shadow"),
        ):
            with mock.patch.object(DispatchResources, "llm", side_effect=ValueError("no key")):
                events = run(graph, "Tell me a joke")
            self.assertEqual(of_type(events, "degraded"), [])
            self.assertNotIn("Not available", of_type(events, "final_answer")[0]["content"])
            self.assertIsNone(decisions(events)[0]["agreed"])


class RiskRetrieval(FakeRetrieval):
    def retrieve(self, query, top_k=5):
        return [SimpleNamespace(source_file="SOP.md", text=f"4. Routes. **{RISK_QUOTE}**.", score=0.6, metadata={})]


def finding(**kw):
    base = dict(evidence="T1", field="Route_Risk_Index", operator=">", threshold=0.6, sop_evidence="S1", sop_quote=RISK_QUOTE)
    return Finding(**{**base, **kw})


class TestThresholds(unittest.TestCase):
    def setUp(self):
        self.by_id = {
            "T1": {"id": "T1", "source": "telemetry", "freshness": "fresh",
                   "fields": {"Route_Risk_Index": 0.7, "Delay_Probability": 0.9, "Current_Temperature_C": 6.8, "Risk_Classification": "Low Risk"}},
            "S1": {"id": "S1", "source": "sop", "origin": "SOP.md", "text": f"4. Routes. **{RISK_QUOTE}**.", "freshness": "undated", "fields": {}},
        }

    def test_valid_extra_finding_is_added_without_actions(self):
        item, reason = check_finding(finding(), self.by_id, set())
        self.assertIsNone(reason)
        self.assertEqual((item["actions"], item["high_stakes"], item["freshness"]), ([], False, "fresh"))

    def test_each_guardrail_rejects(self):
        for bad in (
            finding(evidence="T9"), finding(sop_evidence="T1"), finding(field="Risk_Classification"),
            finding(sop_quote="RT_RSK_IDX above 0.1 requires a route review", threshold=0.1),  # not in the excerpt
            finding(sop_quote="requires a route review"),  # doesn't name the field
            finding(threshold=0.5),  # not the quoted number
            finding(operator="<"),  # 0.7 is not < 0.6: code compares
        ):
            item, reason = check_finding(bad, self.by_id, set())
            self.assertIsNone(item, bad)
            self.assertTrue(reason, bad)

    def test_coded_rules_keep_their_conditions(self):
        # Delay 0.9 on a Low Risk shipment: the rule's risk condition doesn't hold.
        item, reason = check_finding(finding(field="Delay_Probability", threshold=0.65), self.by_id, set())
        self.assertIsNone(item)
        self.assertIn("coded SOP rule does not apply", reason)

    def test_model_cannot_remove_a_coded_breach(self):
        llm = FakeTaskLLM(Assessment(findings=[], reasoning="all fine"))
        events = run(graph_with([GOOD_DRAFT], task_llms={LLMTask.ASSESS: llm}, thresholds_mode="model"), SCAN)
        self.assertEqual(len(of_type(events, "evidence")[0]["triggers"]), 3)
        [d] = decisions(events)
        self.assertFalse(d["agreed"])
        self.assertEqual(len(d["model"]["missed"]), 3)

    def test_port_congestion_outside_the_port_area_stays_out(self):
        inland = (FRESH_BREACH[0], 34.11, -117.29, 2.0, 0.1, "Low Risk", 0.1, 9.0, 0.1)
        llm = FakeTaskLLM(Assessment(reasoning="r", findings=[
            finding(field="Port_Congestion_Level", threshold=7.0, sop_quote="PRT_CNG_LVL exceeds 7.0")]))
        events = run(graph_with(["[T1] congestion is 9 [S1]."], rows=(inland,), task_llms={LLMTask.ASSESS: llm}, thresholds_mode="model"),
                     "Show shipments near San Bernardino")
        evidence = of_type(events, "evidence")[0]
        self.assertEqual(evidence["triggers"], [])
        self.assertIn("ignored 1 model finding", evidence["gaps"][0])

    def test_model_mode_adds_and_shadow_mode_does_not(self):
        row = (*CALM[:8], 0.7)
        for mode, expected in (("model", ["SOP_MODEL_FINDING [T1]"]), ("shadow", [])):
            llm = FakeTaskLLM(Assessment(findings=[finding()], reasoning="r"))
            draft = "[T1] has Route_Risk_Index 0.7, above 0.6 [S1]."
            events = run(graph_with([draft], rows=(row,), retrieval=RiskRetrieval(), task_llms={LLMTask.ASSESS: llm}, thresholds_mode=mode),
                         "Show shipments near Long Beach")
            self.assertEqual(of_type(events, "evidence")[0]["triggers"], expected)
            self.assertEqual(of_type(events, "approval_required"), [])
            self.assertNotIn("SOP TRIGGERS", llm.prompts[0][-1].content)  # not anchored on the coded result

    def test_model_failure_uses_coded_thresholds(self):
        events = run(graph_with([GOOD_DRAFT], task_llms={LLMTask.ASSESS: FakeTaskLLM(AuthenticationError())}, thresholds_mode="model"), SCAN)
        evidence = of_type(events, "evidence")[0]
        self.assertEqual(len(evidence["triggers"]), 3)
        self.assertIn(ASSESS_GAP, evidence["gaps"])

    def test_rules_expose_field_and_direction(self):
        triggers, _ = evaluate_rules([{**self.by_id["T1"], "fields": {"Current_Temperature_C": 6.8}}])
        self.assertEqual((triggers[0]["field"], triggers[0]["direction"]), ("Current_Temperature_C", ">"))


class TestVerification(unittest.TestCase):
    FINE = "[T1] is 2.1 °C."

    def go(self, drafts, reply, mode="model"):
        judge = FakeTaskLLM(reply)
        graph = graph_with(drafts, rows=(CALM,), task_llms={LLMTask.JUDGE: judge}, verification_mode=mode)
        return run(graph, "Show shipments near Long Beach"), judge

    def test_judge_adds_problems_and_triggers_the_retry(self):
        events, _ = self.go([self.FINE, self.FINE], Verdict(passed=False, problems=["[T1] is misread"]))
        verdicts = of_type(events, "verification")
        self.assertEqual([v["passed"] for v in verdicts], [False, False])
        self.assertEqual(verdicts[0]["problems"], ["Model check: [T1] is misread"])
        self.assertIn("**Unverified:**", of_type(events, "final_answer")[0]["content"])

    def test_judge_cannot_clear_a_code_failure(self):
        events, judge = self.go(["[T1] is 99.9 °C.", self.FINE], Verdict(passed=True))
        self.assertEqual([v["passed"] for v in of_type(events, "verification")], [False, True])
        self.assertEqual(len(judge.prompts), 1)  # not asked about the draft code rejected

    def test_shadow_uses_the_code_result(self):
        events, _ = self.go([self.FINE], Verdict(passed=False, problems=["x"]), mode="shadow")
        self.assertTrue(of_type(events, "verification")[0]["passed"])
        self.assertFalse(decisions(events)[0]["agreed"])

    def test_failure_falls_back_to_code_checks(self):
        events, _ = self.go([self.FINE], AuthenticationError())
        self.assertTrue(of_type(events, "verification")[0]["passed"])
        self.assertIn(JUDGE_FALLBACK_GAP, of_type(events, "final_answer")[0]["content"])

    def test_high_stakes_actions_still_stop_for_approval(self):
        graph = graph_with([GOOD_DRAFT], task_llms={LLMTask.JUDGE: FakeTaskLLM(Verdict(passed=True))}, verification_mode="model")
        self.assertEqual(len(of_type(run(graph, SCAN), "approval_required")), 1)


class TestAudit(unittest.TestCase):
    def test_model_decisions_are_audited(self):
        from api.server import generate_dispatch_sse_frames

        rows = []
        steps = [{"type": "model_decision", "decision": "routing", "mode": "shadow", "agreed": False, "code": {}, "model": {}, "reasoning": "r"},
                 {"type": "final_answer", "content": "x", "verified": False, "approval": None}]
        list(generate_dispatch_sse_frames("s", "q", lambda s, p: iter(steps), lambda *a: rows.append(a)))
        self.assertIn(("s", "model", "ModelDecision"), [r[:3] for r in rows])


if __name__ == "__main__":
    unittest.main()
