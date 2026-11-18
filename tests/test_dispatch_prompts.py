"""Prompt templates of the dispatch workflow: variables, rendered messages and injection."""
import unittest
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.runtime import Runtime

from agent.config import AgentSettings
from agent.dispatch.context import DispatchContext, DispatchResources
from agent.dispatch.nodes.planner import planner
from agent.dispatch.nodes.reasoning import reasoner
from agent.dispatch.prompts import (
    gaps_block,
    intent_variables,
    planner_prompt,
    reasoner_prompt,
    retry_block,
)
from agent.dispatch.prompts.reasoner import HISTORY_MESSAGES
from agent.intent import classify_intent

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)


class RecordingLLM:
    def __init__(self, reply=None):
        self.calls, self.reply = [], reply or AIMessage(content="draft [S1]")

    def invoke(self, messages):
        self.calls.append(messages)
        return self.reply


def runtime_with(llm=None, planner_llm=None, **overrides):
    settings = AgentSettings(_env_file=None)
    res = DispatchResources(settings, llm=llm or RecordingLLM(), planner_llm=planner_llm or RecordingLLM())
    return Runtime(context=DispatchContext(settings=settings, resources=res, clock=lambda: NOW, **overrides))


def reasoner_state(**extra):
    state = {
        "messages": [HumanMessage("earlier question"), AIMessage("earlier answer"), HumanMessage("current question")],
        "question": "current question",
        "ledger": [],
        "triggers": [],
        "historical": [],
        "gaps": [],
        "as_of": NOW.isoformat(),
        "attempts": 0,
    }
    state.update(extra)
    return state


class TestTemplateVariables(unittest.TestCase):
    """Catches a stray brace in the prompt text, or a variable the node doesn't pass."""

    def test_reasoner_variables(self):
        self.assertEqual(
            set(reasoner_prompt.input_variables),
            {"question", "ledger", "gaps_block", "retry_block", "freshness_hours", "max_rows"},
        )
        self.assertEqual(reasoner_prompt.optional_variables, ["history"])

    def test_planner_variables(self):
        self.assertEqual(
            set(planner_prompt.input_variables),
            {"question", "max_rows", "place", "coordinates", "area_filter", "temperature_filter", "intent_reason"},
        )
        self.assertEqual(planner_prompt.optional_variables, ["history"])


class TestOptionalBlocks(unittest.TestCase):
    def test_blocks_are_empty_without_content(self):
        self.assertEqual(gaps_block([]), "")
        self.assertEqual(retry_block([]), "")

    def test_blocks_start_with_a_blank_line_and_list_items(self):
        self.assertEqual(
            gaps_block(["sop: down"]),
            "\n\n<data_gaps>\nSay these sources were unavailable; do not guess.\n- sop: down\n</data_gaps>",
        )
        block = retry_block(["Numbers not found: 8.5", "Unknown id [T9]"])
        self.assertTrue(block.startswith("\n\n<verifier_feedback>\nYour previous draft was rejected"))
        self.assertTrue(block.endswith("- Numbers not found: 8.5\n- Unknown id [T9]\n</verifier_feedback>"))


class TestIntentVariables(unittest.TestCase):
    def test_place_and_temperature_become_sql_conditions(self):
        values = intent_variables(classify_intent("Average temperature above 4 °C near Long Beach?"), half_width_deg=0.35)
        self.assertEqual(values["place"], "long beach")
        self.assertEqual(values["coordinates"], "33.77, -118.19")
        self.assertEqual(values["area_filter"], "Latitude BETWEEN 33.4200 AND 34.1200 AND Longitude BETWEEN -118.5400 AND -117.8400")
        self.assertEqual(values["temperature_filter"], "Current_Temperature_C > 4")

    def test_missing_fields_are_none(self):
        values = intent_variables(classify_intent("What is the average temperature per risk class?"), half_width_deg=0.35)
        self.assertEqual({k: values[k] for k in ("place", "coordinates", "area_filter", "temperature_filter")},
                         dict.fromkeys(("place", "coordinates", "area_filter", "temperature_filter"), "none"))


class TestRenderedMessages(unittest.TestCase):
    def test_planner_sends_system_history_and_question(self):
        recorder = RecordingLLM(AIMessage(content="", tool_calls=[]))
        question = "How many High Risk shipments are near Long Beach?"
        state = {"messages": reasoner_state()["messages"], "question": question, "intent": classify_intent(question).to_state()}
        planner(state, runtime_with(planner_llm=recorder))
        [msgs] = recorder.calls
        self.assertEqual([type(m) for m in msgs], [SystemMessage, HumanMessage, AIMessage, HumanMessage])
        self.assertIn("SELECT TOP 10", msgs[0].content)  # max_rows from settings
        human = msgs[-1].content
        self.assertTrue(human.startswith(f"Question to plan for: {question}\n"))
        self.assertIn("Area filter: Latitude BETWEEN 33.4200 AND 34.1200", human)
        self.assertIn("Routing reason: aggregate or comparison question", human)

    def test_reasoner_first_attempt_has_ledger_and_no_retry_feedback(self):
        llm = RecordingLLM()
        out = reasoner(reasoner_state(), runtime_with(llm=llm))
        [msgs] = llm.calls
        self.assertIn("at most 24 hours old", msgs[0].content)
        self.assertIn("(at most 10 rows)", msgs[0].content)
        self.assertEqual([m.content for m in msgs[1:3]], ["earlier question", "earlier answer"])  # current question not repeated
        human = msgs[-1].content
        self.assertTrue(human.startswith(
            "<question>\ncurrent question\n</question>\n\n<evidence_ledger_data>\nEVIDENCE LEDGER (now 2026-11-18 22:00 UTC"
        ))
        self.assertTrue(human.endswith("</evidence_ledger_data>"))  # no optional blocks
        self.assertEqual(out, {"draft": "draft [S1]", "attempts": 1})

    def test_reasoner_retry_includes_gaps_and_verifier_feedback(self):
        llm = RecordingLLM()
        state = reasoner_state(attempts=1, gaps=["weather: down"], verification={"passed": False, "problems": ["Numbers not found: 8.5"]})
        reasoner(state, runtime_with(llm=llm))
        human = llm.calls[0][-1].content
        self.assertLess(human.index("<data_gaps>"), human.index("<verifier_feedback>"))
        self.assertTrue(human.endswith("- Numbers not found: 8.5\n</verifier_feedback>"))

    def test_history_is_capped(self):
        llm = RecordingLLM()
        turns = [HumanMessage(f"q{i}") if i % 2 == 0 else AIMessage(f"a{i}") for i in range(10)] + [HumanMessage("current question")]
        reasoner(reasoner_state(messages=turns), runtime_with(llm=llm))
        self.assertEqual(len(llm.calls[0]), 1 + HISTORY_MESSAGES + 1)

    def test_problems_from_a_previous_turn_are_ignored_on_a_first_attempt(self):
        llm = RecordingLLM()
        reasoner(reasoner_state(verification={"passed": False, "problems": ["stale"]}), runtime_with(llm=llm))
        self.assertNotIn("stale", llm.calls[0][-1].content)


class TestInjection(unittest.TestCase):
    def test_context_templates_can_be_replaced(self):
        llm = RecordingLLM()
        custom = ChatPromptTemplate.from_messages(
            [("system", "custom system {max_rows}"), ("human", "{question}|{ledger}{gaps_block}{retry_block}{freshness_hours}")]
        )
        reasoner(reasoner_state(), runtime_with(llm=llm, reasoner_prompt=custom))
        [msgs] = llm.calls
        self.assertEqual(msgs[0].content, "custom system 10")
        self.assertTrue(msgs[1].content.startswith("current question|EVIDENCE LEDGER"))

    def test_default_context_uses_the_module_templates(self):
        settings = AgentSettings(_env_file=None)
        ctx = DispatchContext.build(settings, DispatchResources(settings))
        self.assertIs(ctx.reasoner_prompt, reasoner_prompt)
        self.assertIs(ctx.planner_prompt, planner_prompt)


if __name__ == "__main__":
    unittest.main()
