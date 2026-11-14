"""Retry policy for calls to external services (embedding APIs, Pinecone).

Only *transient* failures (timeouts, connection resets, 429/5xx) are retried, with jittered
exponential backoff. Permanent errors (bad request, auth, dimension mismatch) fail fast.
"""
from __future__ import annotations

import logging

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRANSIENT_NAME_HINTS = ("Timeout", "Connection", "RateLimit", "ServiceUnavailable", "MaxRetry", "Protocol")


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in TRANSIENT_STATUS_CODES
    name = type(exc).__name__
    return any(hint in name for hint in _TRANSIENT_NAME_HINTS)


def with_retry(attempts: int = 5, max_wait: float = 30.0, predicate=is_transient):
    """Decorator: retry transient failures. Use small values on latency-sensitive paths."""
    return retry(
        retry=retry_if_exception(predicate),
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=0.5, max=max_wait),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

