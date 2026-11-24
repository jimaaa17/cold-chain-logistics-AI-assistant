"""Asks a model about a decision code also makes (routing, thresholds, verification).

In `shadow` mode nothing about the model call may affect the answer: no gap, no `degraded` event, no
retry, and a client that can't be built counts as a failed call. In `model` mode the Phase 6 failure
policy applies (retry once, then the coded result with a gap).
"""
from __future__ import annotations

from typing import Any, TypeVar

from langgraph.runtime import Runtime

from agent.dispatch.context import DispatchContext
from agent.dispatch.resilience import invoke_structured, run_model_step

T = TypeVar("T")


def consult(
    runtime: Runtime[DispatchContext], *, decision: str, mode: str, task: Any, schema: type[T], messages: list,
    fallback_name: str, gap: str,
) -> tuple[T | None, list[str]]:
    """Returns (model output or None if it failed, gaps to record)."""
    resources = runtime.context.resources
    if mode == "shadow":
        result = run_model_step(
            decision, lambda: invoke_structured(resources.task_llm(task), schema, messages), lambda: None,
            emit=lambda event: None, fallback_name=fallback_name, gap=gap, retry=False,
        )
        if result.degraded:
            emit_decision(runtime, decision=decision, mode=mode, agreed=None, code=None, model=None, reasoning="model call failed")
        return result.value, []
    llm = resources.task_llm(task)  # a missing key is a configuration error, not a fallback
    result = run_model_step(
        decision, lambda: invoke_structured(llm, schema, messages), lambda: None,
        emit=runtime.stream_writer, fallback_name=fallback_name, gap=gap,
    )
    return result.value, result.gaps


def emit_decision(runtime: Runtime[DispatchContext], *, decision: str, mode: str, agreed: bool | None, code: Any, model: Any, reasoning: str = "") -> None:
    runtime.stream_writer({
        "type": "model_decision", "decision": decision, "mode": mode, "agreed": agreed,
        "code": code, "model": model, "reasoning": reasoning[:500],
    })
