"""Prompts for the dispatch workflow, one module per step. Templates are rendered by the nodes with
`format_messages(...)`; settings reach them as variables at render time, never at import time."""
from agent.dispatch.prompts.clarify import CLARIFY_TEXT
from agent.dispatch.prompts.planner import PLANNER_SYSTEM, planner_prompt
from agent.dispatch.prompts.reasoner import REASONER_SYSTEM, gaps_block, reasoner_prompt, retry_block

__all__ = [
    "CLARIFY_TEXT",
    "PLANNER_SYSTEM",
    "REASONER_SYSTEM",
    "gaps_block",
    "planner_prompt",
    "reasoner_prompt",
    "retry_block",
]
