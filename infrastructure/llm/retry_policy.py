# infrastructure/llm/retry_policy.py
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 30.0


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_RETRYABLE_HTTPX_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)

_RETRYABLE_EXC_CLASSNAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
    "RateLimitError",
    "APIStatusError",
    "ServerError",
    "DeadlineExceeded",
    "UnavailableError",
}

_RETRYABLE_ERROR_SUBSTRINGS = (
    "server disconnected",
    "connection aborted",
    "connection reset",
    "broken pipe",
    "eof occurred",
    "temporarily unavailable",
)


def _extract_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _extract_retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE_HTTPX_EXCEPTIONS):
        return True
    status = _extract_status_code(exc)
    if status is not None:
        return status in _RETRYABLE_STATUS_CODES
    for cls in type(exc).__mro__:
        if cls.__name__ in _RETRYABLE_EXC_CLASSNAMES:
            return True
    message = str(exc).lower()
    for needle in _RETRYABLE_ERROR_SUBSTRINGS:
        if needle in message:
            return True
    return False


def _compute_sleep(
    *,
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None,
) -> float:
    exponential = policy.backoff_base_s * (2 ** (attempt - 1))
    exponential = min(exponential, policy.backoff_cap_s)
    jitter = random.uniform(0.0, 0.25)
    sleep_s = exponential * (1 + jitter)
    if retry_after is not None:
        sleep_s = max(sleep_s, min(retry_after, policy.backoff_cap_s))
    return sleep_s


def run_with_retry(
    *,
    provider: str,
    operation: Callable[[], T],
    policy: RetryPolicy,
) -> T:
    total_attempts = 1 + policy.max_retries
    last_exc: BaseException | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            if attempt > 1:
                logger.info(
                    "[llm-retry] %s intento %s/%s",
                    provider, attempt, total_attempts,
                )
            return operation()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            last_exc = exc
            retryable = _is_retryable(exc)
            is_last_attempt = attempt >= total_attempts
            if not retryable:
                logger.warning(
                    "[llm-retry] %s error NO retryable. type=%s msg=%s",
                    provider, type(exc).__name__, str(exc)[:300],
                )
                raise
            if is_last_attempt:
                logger.error(
                    "[llm-retry] %s agotados reintentos (%s). type=%s msg=%s",
                    provider, total_attempts,
                    type(exc).__name__, str(exc)[:300],
                )
                raise
            retry_after = _extract_retry_after(exc)
            sleep_s = _compute_sleep(
                attempt=attempt, policy=policy, retry_after=retry_after,
            )
            logger.warning(
                "[llm-retry] %s intento %s/%s falló retryable "
                "(type=%s msg=%s). Esperando %.2fs…",
                provider, attempt, total_attempts,
                type(exc).__name__, str(exc)[:200], sleep_s,
            )
            time.sleep(sleep_s)

    assert last_exc is not None
    raise last_exc
