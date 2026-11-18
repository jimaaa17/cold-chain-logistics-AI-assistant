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
    PLANNER_SYSTEM,
    REASONER_SYSTEM,
    gaps_block,
    planner_prompt,
    reasoner_prompt,
    retry_block,
)

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
        self.assertEqual(set(reasoner_prompt.input_variables), {"question", "ledger", "gaps_block", "retry_block"})
        self.assertEqual(reasoner_prompt.optional_variables, ["history"])

    def test_planner_variables(self):
        self.assertEqual(set(planner_prompt.input_variables), {"question"})
        self.assertEqual(planner_prompt.optional_variables, ["history"])


class TestOptionalBlocks(unittest.TestCase):
    def test_blocks_are_empty_without_content(self):
        self.assertEqual(gaps_block([]), "")
        self.assertEqual(retry_block([]), "")

    def test_blocks_start_with_a_blank_line_and_list_items(self):
        self.assertEqual(gaps_block(["sop: down"]), "\n\nDATA GAPS (say these sources were unavailable; do not guess):\n- sop: down")
        block = retry_block(["Numbers not found: 8.5", "Unknown id [T9]"])
        self.assertTrue(block.startswith("\n\nYOUR PREVIOUS DRAFT WAS REJECTED"))
        self.assertTrue(block.endswith("- Numbers not found: 8.5\n- Unknown id [T9]"))


class TestRenderedMessages(unittest.TestCase):
    def test_planner_sends_system_history_and_question(self):
        recorder = RecordingLLM(AIMessage(content="", tool_calls=[]))
        state = {"messages": reasoner_state()["messages"], "question": "current question"}
        planner(state, runtime_with(planner_llm=recorder))
        [msgs] = recorder.calls
        self.assertEqual([type(m) for m in msgs], [SystemMessage, HumanMessage, AIMessage, HumanMessage])
        self.assertEqual(msgs[0].content, PLANNER_SYSTEM)
        self.assertEqual(msgs[-1].content, "current question")

    def test_reasoner_first_attempt_has_ledger_and_no_retry_feedback(self):
        llm = RecordingLLM()
        out = reasoner(reasoner_state(), runtime_with(llm=llm))
        [msgs] = llm.calls
        self.assertEqual(msgs[0].content, REASONER_SYSTEM)
        self.assertEqual([m.content for m in msgs[1:3]], ["earlier question", "earlier answer"])  # current question not repeated
        human = msgs[-1].content
        self.assertTrue(human.startswith("QUESTION:\ncurrent question\n\nEVIDENCE LEDGER (now 2026-11-18 22:00 UTC"))
        self.assertNotIn("DATA GAPS", human)
        self.assertNotIn("REJECTED", human)
        self.assertEqual(out, {"draft": "draft [S1]", "attempts": 1})

    def test_reasoner_retry_includes_gaps_and_verifier_feedback(self):
        llm = RecordingLLM()
        state = reasoner_state(attempts=1, gaps=["weather: down"], verification={"passed": False, "problems": ["Numbers not found: 8.5"]})
        reasoner(state, runtime_with(llm=llm))
        human = llm.calls[0][-1].content
        self.assertLess(human.index("DATA GAPS"), human.index("REJECTED BY THE AUTOMATIC CHECK"))
        self.assertTrue(human.endswith("- Numbers not found: 8.5"))

    def test_problems_from_a_previous_turn_are_ignored_on_a_first_attempt(self):
        llm = RecordingLLM()
        reasoner(reasoner_state(verification={"passed": False, "problems": ["stale"]}), runtime_with(llm=llm))
        self.assertNotIn("stale", llm.calls[0][-1].content)


class TestInjection(unittest.TestCase):
    def test_context_templates_can_be_replaced(self):
        llm = RecordingLLM()
        custom = ChatPromptTemplate.from_messages(
            [("system", "custom system"), ("human", "{question}|{ledger}{gaps_block}{retry_block}")]
        )
        reasoner(reasoner_state(), runtime_with(llm=llm, reasoner_prompt=custom))
        [msgs] = llm.calls
        self.assertEqual(msgs[0].content, "custom system")
        self.assertTrue(msgs[1].content.startswith("current question|EVIDENCE LEDGER"))

    def test_default_context_uses_the_module_templates(self):
        settings = AgentSettings(_env_file=None)
        ctx = DispatchContext.build(settings, DispatchResources(settings))
        self.assertIs(ctx.reasoner_prompt, reasoner_prompt)
        self.assertIs(ctx.planner_prompt, planner_prompt)


if __name__ == "__main__":
    unittest.main()
