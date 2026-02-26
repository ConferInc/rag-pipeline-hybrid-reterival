"""
LLM retry with exponential backoff.

Retries only on retriable exceptions (429, timeout, connection, 5xx).
Does NOT retry on 401, 400, 404, etc.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import openai

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Retriable: rate limit (429), timeout, connection, server errors (5xx)
_RETRIABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


def _is_retriable(e: BaseException) -> bool:
    """Return True if exception is retriable."""
    for ex_type in _RETRIABLE_EXCEPTIONS:
        if isinstance(e, ex_type):
            return True
    return False


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    initial_delay_ms: float = 1000,
    max_delay_ms: float = 30000,
    jitter: bool = True,
) -> T:
    """
    Execute fn with retries on retriable exceptions.

    Args:
        fn: No-arg callable (e.g. lambda: client.chat.completions.create(...))
        max_attempts: Total attempts (1 = no retries)
        initial_delay_ms: Initial delay before first retry
        max_delay_ms: Cap on delay between retries
        jitter: Add random jitter to delay

    Returns:
        Result of fn()

    Raises:
        Last exception if all attempts fail
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except BaseException as e:
            last_exc = e
            if not _is_retriable(e) or attempt >= max_attempts - 1:
                raise
            delay_ms = min(
                initial_delay_ms * (2 ** attempt),
                max_delay_ms,
            )
            if jitter:
                delay_ms = delay_ms * (0.5 + random.random())
            delay_s = delay_ms / 1000
            logger.warning(
                "LLM call failed, retrying",
                extra={
                    "attempt": attempt + 1,
                    "max_attempts": max_attempts,
                    "error": str(e),
                    "delay_seconds": round(delay_s, 2),
                },
            )
            time.sleep(delay_s)
    if last_exc:
        raise last_exc
    raise RuntimeError("Unreachable")
