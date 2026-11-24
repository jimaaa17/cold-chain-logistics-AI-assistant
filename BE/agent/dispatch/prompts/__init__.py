"""Prompts for the dispatch workflow, one module per step. Templates are rendered by the nodes with
`format_messages(...)`; settings reach them as variables at render time, never at import time."""
from agent.dispatch.prompts.assessor import assessor_prompt
from agent.dispatch.prompts.clarify import CLARIFY_TEXT
from agent.dispatch.prompts.judge import judge_prompt
from agent.dispatch.prompts.planner import PLANNER_SYSTEM, intent_variables, planner_prompt
from agent.dispatch.prompts.reasoner import REASONER_SYSTEM, gaps_block, reasoner_prompt, retry_block
from agent.dispatch.prompts.router import router_prompt

__all__ = [
    "CLARIFY_TEXT",
    "PLANNER_SYSTEM",
    "REASONER_SYSTEM",
    "assessor_prompt",
    "gaps_block",
    "intent_variables",
    "judge_prompt",
    "planner_prompt",
    "reasoner_prompt",
    "retry_block",
    "router_prompt",
]
