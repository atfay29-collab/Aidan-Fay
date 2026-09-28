"""HTTP plumbing shared by the API clients: throttling, retries, typed errors."""
from __future__ import annotations

import email.utils
import logging
import random
import time

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """A request failed in a way that affects only this lookup."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    """Bad or missing API key. Every later request would fail too."""


class QuotaExceeded(ApiError):
    """Out of credits, or still rate limited after all retries. Stop using this provider."""


class RateLimiter:
    """Enforces a minimum gap between consecutive calls to one provider."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval:
            remaining = self._last + self.min_interval - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


def retry_after_seconds(response: requests.Response) -> float | None:
    """Parse a Retry-After header given either as seconds or as an HTTP date."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    limiter: RateLimiter | None = None,
    max_retries: int = 5,
    backoff_base: float = 2.0,
    max_backoff: float = 120.0,
    timeout: float = 30.0,
    provider: str = "API",
    **kwargs,
) -> requests.Response:
    """Send a request, retrying rate limits (429), 5xx errors and network failures.

    Waits for Retry-After when the server sends it, otherwise backs off
    exponentially with jitter. Returns the final response whatever its status;
    callers decide what a 404 or 402 means for their provider.
    """
    attempt = 0
    while True:
        if limiter:
            limiter.wait()
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= max_retries:
                raise ApiError(f"{provider}: network error after {attempt + 1} attempts: {exc}") from exc
            delay = min(max_backoff, backoff_base * 2**attempt) + random.uniform(0, 1)
            log.warning("%s: network error (%s); retrying in %.1fs", provider, exc, delay)
            time.sleep(delay)
            attempt += 1
            continue

        if response.status_code in RETRY_STATUSES and attempt < max_retries:
            delay = retry_after_seconds(response)
            if delay is None:
                delay = min(max_backoff, backoff_base * 2**attempt) + random.uniform(0, 1)
            delay = min(delay, max_backoff)
            log.warning(
                "%s: HTTP %s (attempt %d/%d); retrying in %.1fs",
                provider,
                response.status_code,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            time.sleep(delay)
            attempt += 1
            continue
        return response


def error_message(response: requests.Response) -> str:
    """Pull a readable message out of a provider's JSON error body."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300]
    if isinstance(body, dict):
        error = body.get("error") or body.get("errors")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("details") or error)
        if isinstance(error, list) and error:
            first = error[0]
            if isinstance(first, dict):
                return str(first.get("details") or first.get("message") or first)
            return str(first)
        if error:
            return str(error)
        if body.get("message"):
            return str(body["message"])
    return str(body)[:300]
