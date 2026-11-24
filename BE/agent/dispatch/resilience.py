"""One failure policy for model calls in the dispatch workflow.

A failing model call never ends the turn. `run_model_step` retries a transient failure (429, 5xx,
timeout, connection) once and otherwise returns the step's fallback. The retry lives here because a
node-level LangGraph `RetryPolicy` would never see an exception this function has already handled.
Permanent errors, unparseable structured output and empty replies fall back straight away.

Each fallback records one gap and emits an audited `degraded` event carrying the exception type only
(messages can hold connection details or keys). Build the client and format the prompt *before*
calling this: a missing API key or a broken template must surface as an error, not as a fallback.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from rag.core.resilience import TRANSIENT_STATUS_CODES, is_transient

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRY_WAIT_S = 1.0


class EmptyModelReply(RuntimeError):
    """The model answered with nothing usable (empty text, content filter, no structured output)."""


def is_transient_model_error(exc: BaseException) -> bool:
    """`rag.core.resilience.is_transient`, plus the `code` attribute Google's SDK errors carry."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in TRANSIENT_STATUS_CODES
    return is_transient(exc)


@dataclass
class StepResult(Generic[T]):
    value: T
    degraded: bool = False
    gaps: list[str] = field(default_factory=list)


def run_model_step(
    step: str,
    call: Callable[[], T],
    fallback: Callable[[], T],
    *,
    emit: Callable[[dict], None],
    fallback_name: str,
    gap: str,
    retry: bool = True,
) -> StepResult[T]:
    """Runs `call`; on failure returns `fallback()` with `gap`, after emitting a `degraded` event."""
    try:
        try:
            return StepResult(call())
        except Exception as exc:
            if not (retry and is_transient_model_error(exc)):
                raise
            logger.warning("Model step %s: transient %s, retrying once", step, type(exc).__name__)
            time.sleep(RETRY_WAIT_S)
            return StepResult(call())
    except Exception as exc:
        reason = type(exc).__name__
        logger.warning("Model step %s failed (%s); using %s", step, reason, fallback_name)
        emit({"type": "degraded", "step": step, "fallback": fallback_name, "reason": reason})
        return StepResult(fallback(), degraded=True, gaps=[gap])


def invoke_structured(llm: Any, schema: type[T], messages: list) -> T:
    """Structured output as a `schema` instance; raises on an empty or invalid reply."""
    out = llm.with_structured_output(schema).invoke(messages)
    if out is None:
        raise EmptyModelReply("no structured output")
    return out if isinstance(out, schema) else schema.model_validate(out)  # type: ignore[attr-defined]
