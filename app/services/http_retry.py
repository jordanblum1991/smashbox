"""Bounded exponential-backoff retry for transient sync HTTP failures.

The sync fetchers (TikTok Shop, TikTok Marketing, SAP inventory) previously had
NO retry: a single timeout / 429 / 5xx aborted the whole stream until the next
scheduled run — up to a 24h gap for one transient hiccup. `send_with_retry`
wraps a zero-arg ``send`` that performs ONE httpx request and returns its
Response, retrying on transport errors (timeouts, connection resets) and on
retryable HTTP status codes (429 + 5xx).

A non-retryable response (2xx, or 4xx other than 429) is returned as-is for the
caller to unwrap — so existing ``raise_for_status`` / ``_unwrap`` error handling
is unchanged. After exhausting retries on a retryable *status*, the final
response is likewise returned (the caller then raises as before); only a
transport error on the last attempt re-raises. Backoff is deterministic
(``base_delay`` doubling each retry) and ``sleep`` is injectable so tests don't
wait.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

# 429 (rate limit) + 5xx (server/gateway) are transient; 4xx are real errors.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5  # seconds; doubles each retry (0.5, 1.0, 2.0, …)


def send_with_retry(
    send: Callable[[], httpx.Response],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "request",
) -> httpx.Response:
    """Call ``send`` up to ``attempts`` times, backing off between tries on
    transient failures. Returns the final Response (retryable or not); re-raises
    only a transport error that persists to the last attempt."""
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            resp = send()
        except httpx.TransportError as exc:  # timeouts, connect/read/network errors
            if last:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning("retrying %s after transport error (attempt %d/%d): %s",
                           label, attempt + 1, attempts, exc)
            sleep(delay)
            continue
        if resp.status_code in RETRYABLE_STATUS and not last:
            delay = base_delay * (2 ** attempt)
            logger.warning("retrying %s after HTTP %d (attempt %d/%d)",
                           label, resp.status_code, attempt + 1, attempts)
            sleep(delay)
            continue
        return resp
    # Unreachable: the loop either returns or raises on the last attempt.
    raise RuntimeError("send_with_retry: exhausted attempts without returning")
